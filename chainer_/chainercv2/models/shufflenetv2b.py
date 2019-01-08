"""
    ShuffleNet V2, implemented in Chainer. The alternative version.
    Original paper: 'ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design,'
    https://arxiv.org/abs/1807.11164.
"""

__all__ = ['ShuffleNetV2b', 'shufflenetv2b_wd2', 'shufflenetv2b_w1', 'shufflenetv2b_w3d2', 'shufflenetv2b_w2',
           'shufflenetv2c_wd2', 'shufflenetv2c_w1']

import os
import chainer.functions as F
import chainer.links as L
from chainer import Chain
from functools import partial
from chainer.serializers import load_npz
from .common import conv1x1_block, conv3x3_block, ChannelShuffle, ChannelShuffle2, SEBlock, SimpleSequential


class ShuffleDwConv3x3(Chain):
    """
    ShuffleNetV2(b) specific depthwise convolution 3x3 layer.

    Parameters:
    ----------
    channels : int
        Number of input/output channels.
    stride : int or tuple/list of 2 int
        Stride of the convolution.
    """
    def __init__(self,
                 channels,
                 stride):
        super(ShuffleDwConv3x3, self).__init__()
        with self.init_scope():
            self.conv = L.Convolution2D(
                in_channels=channels,
                out_channels=channels,
                ksize=3,
                stride=stride,
                pad=1,
                nobias=True,
                groups=channels)
            self.bn = L.BatchNormalization(
                size=channels,
                eps=1e-5)

    def __call__(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return x


class ShuffleUnit(Chain):
    """
    ShuffleNetV2(b) unit.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    downsample : bool
        Whether do downsample.
    use_se : bool
        Whether to use SE block.
    use_residual : bool
        Whether to use residual connection.
    shuffle_group_first : bool
        Whether to use channel shuffle in group first mode.
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 downsample,
                 use_se,
                 use_residual,
                 shuffle_group_first):
        super(ShuffleUnit, self).__init__()
        self.downsample = downsample
        self.use_se = use_se
        self.use_residual = use_residual
        mid_channels = out_channels // 2
        in_channels2 = in_channels // 2
        assert (in_channels % 2 == 0)

        y2_in_channels = (in_channels if downsample else in_channels2)
        y2_out_channels = out_channels - y2_in_channels

        with self.init_scope():
            self.conv1 = conv1x1_block(
                in_channels=y2_in_channels,
                out_channels=mid_channels)
            self.dconv = ShuffleDwConv3x3(
                channels=mid_channels,
                stride=(2 if self.downsample else 1))
            self.conv2 = conv1x1_block(
                in_channels=mid_channels,
                out_channels=y2_out_channels)
            if self.use_se:
                self.se = SEBlock(channels=y2_out_channels)
            if downsample:
                self.shortcut_dconv = ShuffleDwConv3x3(
                    channels=in_channels,
                    stride=2)
                self.shortcut_conv = conv1x1_block(
                    in_channels=in_channels,
                    out_channels=in_channels)

            if shuffle_group_first:
                self.c_shuffle = ChannelShuffle(
                    channels=out_channels,
                    groups=2)
            else:
                self.c_shuffle = ChannelShuffle2(
                    channels=out_channels,
                    groups=2)

    def __call__(self, x):
        if self.downsample:
            y1 = self.shortcut_dconv(x)
            y1 = self.shortcut_conv(y1)
            x2 = x
        else:
            y1, x2 = F.split_axis(x, indices_or_sections=2, axis=1)
        y2 = self.conv1(x2)
        y2 = self.dconv(y2)
        y2 = self.conv2(y2)
        if self.use_se:
            y2 = self.se(y2)
        if self.use_residual and not self.downsample:
            y2 = y2 + x2
        x = F.concat((y1, y2), axis=1)
        x = self.c_shuffle(x)
        return x


class ShuffleInitBlock(Chain):
    """
    ShuffleNetV2(b) specific initial block.

    Parameters:
    ----------
    in_channels : int
        Number of input channels.
    out_channels : int
        Number of output channels.
    """
    def __init__(self,
                 in_channels,
                 out_channels):
        super(ShuffleInitBlock, self).__init__()
        with self.init_scope():
            self.conv = conv3x3_block(
                in_channels=in_channels,
                out_channels=out_channels,
                stride=2)
            self.pool = partial(
                F.max_pooling_2d,
                ksize=3,
                stride=2,
                pad=1,
                cover_all=False)

    def __call__(self, x):
        x = self.conv(x)
        x = self.pool(x)
        return x


class ShuffleNetV2b(Chain):
    """
    ShuffleNetV2(b) model from 'ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design,'
    https://arxiv.org/abs/1807.11164.

    Parameters:
    ----------
    channels : list of list of int
        Number of output channels for each unit.
    init_block_channels : int
        Number of output channels for the initial unit.
    final_block_channels : int
        Number of output channels for the final block of the feature extractor.
    use_se : bool, default False
        Whether to use SE block.
    use_residual : bool, default False
        Whether to use residual connections.
    shuffle_group_first : bool, default True
        Whether to use channel shuffle in group first mode.
    in_channels : int, default 3
        Number of input channels.
    in_size : tuple of two ints, default (224, 224)
        Spatial size of the expected input image.
    classes : int, default 1000
        Number of classification classes.
    """
    def __init__(self,
                 channels,
                 init_block_channels,
                 final_block_channels,
                 use_se=False,
                 use_residual=False,
                 shuffle_group_first=True,
                 in_channels=3,
                 in_size=(224, 224),
                 classes=1000):
        super(ShuffleNetV2b, self).__init__()
        self.in_size = in_size
        self.classes = classes

        with self.init_scope():
            self.features = SimpleSequential()
            with self.features.init_scope():
                setattr(self.features, "init_block", ShuffleInitBlock(
                    in_channels=in_channels,
                    out_channels=init_block_channels))
                in_channels = init_block_channels
                for i, channels_per_stage in enumerate(channels):
                    stage = SimpleSequential()
                    with stage.init_scope():
                        for j, out_channels in enumerate(channels_per_stage):
                            downsample = (j == 0)
                            setattr(stage, "unit{}".format(j + 1), ShuffleUnit(
                                in_channels=in_channels,
                                out_channels=out_channels,
                                downsample=downsample,
                                use_se=use_se,
                                use_residual=use_residual,
                                shuffle_group_first=shuffle_group_first))
                            in_channels = out_channels
                    setattr(self.features, "stage{}".format(i + 1), stage)
                setattr(self.features, 'final_block', conv1x1_block(
                    in_channels=in_channels,
                    out_channels=final_block_channels))
                in_channels = final_block_channels
                setattr(self.features, 'final_pool', partial(
                    F.average_pooling_2d,
                    ksize=7,
                    stride=1))

            self.output = SimpleSequential()
            with self.output.init_scope():
                setattr(self.output, 'flatten', partial(
                    F.reshape,
                    shape=(-1, in_channels)))
                setattr(self.output, 'fc', L.Linear(
                    in_size=in_channels,
                    out_size=classes))

    def __call__(self, x):
        x = self.features(x)
        x = self.output(x)
        return x


def get_shufflenetv2b(width_scale,
                      shuffle_group_first=True,
                      model_name=None,
                      pretrained=False,
                      root=os.path.join('~', '.chainer', 'models'),
                      **kwargs):
    """
    Create ShuffleNetV2(b) model with specific parameters.

    Parameters:
    ----------
    width_scale : float
        Scale factor for width of layers.
    shuffle_group_first : bool, default True
        Whether to use channel shuffle in group first mode.
    model_name : str or None, default None
        Model name for loading pretrained model.
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """

    init_block_channels = 24
    final_block_channels = 1024
    layers = [4, 8, 4]
    channels_per_layers = [116, 232, 464]

    channels = [[ci] * li for (ci, li) in zip(channels_per_layers, layers)]

    if width_scale != 1.0:
        channels = [[int(cij * width_scale) for cij in ci] for ci in channels]
        if width_scale > 1.5:
            final_block_channels = int(final_block_channels * width_scale)

    net = ShuffleNetV2b(
        channels=channels,
        init_block_channels=init_block_channels,
        final_block_channels=final_block_channels,
        shuffle_group_first=shuffle_group_first,
        **kwargs)

    if pretrained:
        if (model_name is None) or (not model_name):
            raise ValueError("Parameter `model_name` should be properly initialized for loading pretrained model.")
        from .model_store import get_model_file
        load_npz(
            file=get_model_file(
                model_name=model_name,
                local_model_store_dir_path=root),
            obj=net)

    return net


def shufflenetv2b_wd2(**kwargs):
    """
    ShuffleNetV2(b) 0.5x model from 'ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design,'
    https://arxiv.org/abs/1807.11164.

    Parameters:
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_shufflenetv2b(
        width_scale=(12.0 / 29.0),
        shuffle_group_first=True,
        model_name="shufflenetv2b_wd2",
        **kwargs)


def shufflenetv2b_w1(**kwargs):
    """
    ShuffleNetV2(b) 1x model from 'ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design,'
    https://arxiv.org/abs/1807.11164.

    Parameters:
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_shufflenetv2b(
        width_scale=1.0,
        shuffle_group_first=True,
        model_name="shufflenetv2b_w1",
        **kwargs)


def shufflenetv2b_w3d2(**kwargs):
    """
    ShuffleNetV2(b) 1.5x model from 'ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design,'
    https://arxiv.org/abs/1807.11164.

    Parameters:
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_shufflenetv2b(
        width_scale=(44.0 / 29.0),
        shuffle_group_first=True,
        model_name="shufflenetv2b_w3d2",
        **kwargs)


def shufflenetv2b_w2(**kwargs):
    """
    ShuffleNetV2(b) 2x model from 'ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design,'
    https://arxiv.org/abs/1807.11164.

    Parameters:
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_shufflenetv2b(
        width_scale=(61.0 / 29.0),
        shuffle_group_first=True,
        model_name="shufflenetv2b_w2",
        **kwargs)


def shufflenetv2c_wd2(**kwargs):
    """
    ShuffleNetV2(c) 0.5x model from 'ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design,'
    https://arxiv.org/abs/1807.11164.

    Parameters:
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_shufflenetv2b(
        width_scale=(12.0 / 29.0),
        shuffle_group_first=False,
        model_name="shufflenetv2c_wd2",
        **kwargs)


def shufflenetv2c_w1(**kwargs):
    """
    ShuffleNetV2(c) 1x model from 'ShuffleNet V2: Practical Guidelines for Efficient CNN Architecture Design,'
    https://arxiv.org/abs/1807.11164.

    Parameters:
    ----------
    pretrained : bool, default False
        Whether to load the pretrained weights for model.
    root : str, default '~/.chainer/models'
        Location for keeping the model parameters.
    """
    return get_shufflenetv2b(
        width_scale=1.0,
        shuffle_group_first=False,
        model_name="shufflenetv2c_w1",
        **kwargs)


def _test():
    import numpy as np
    import chainer

    chainer.global_config.train = False

    pretrained = False

    models = [
        shufflenetv2b_wd2,
        shufflenetv2b_w1,
        shufflenetv2b_w3d2,
        shufflenetv2b_w2,
        shufflenetv2c_wd2,
        shufflenetv2c_w1,
    ]

    for model in models:

        net = model(pretrained=pretrained)
        weight_count = net.count_params()
        print("m={}, {}".format(model.__name__, weight_count))
        assert (model != shufflenetv2b_wd2 or weight_count == 1366792)
        assert (model != shufflenetv2b_w1 or weight_count == 2279760)
        assert (model != shufflenetv2b_w3d2 or weight_count == 4410194)
        assert (model != shufflenetv2b_w2 or weight_count == 7611290)
        assert (model != shufflenetv2c_wd2 or weight_count == 1366792)
        assert (model != shufflenetv2c_w1 or weight_count == 2279760)

        x = np.zeros((1, 3, 224, 224), np.float32)
        y = net(x)
        assert (y.shape == (1, 1000))


if __name__ == "__main__":
    _test()
