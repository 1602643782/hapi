#  Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserve.
#
#Licensed under the Apache License, Version 2.0 (the "License");
#you may not use this file except in compliance with the License.
#You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#Unless required by applicable law or agreed to in writing, software
#distributed under the License is distributed on an "AS IS" BASIS,
#WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#See the License for the specific language governing permissions and
#limitations under the License.

import paddle.fluid as fluid
from paddle.fluid.param_attr import ParamAttr
from paddle.fluid.regularizer import L2Decay

from paddle.fluid.dygraph.nn import Conv2D, BatchNorm

from paddle.incubate.hapi.model import Model
from paddle.incubate.hapi.download import get_weights_path_from_url

__all__ = ['DarkNet', 'darknet53']

# {num_layers: (url, md5)}
pretrain_infos = {
        53: ('https://paddlemodels.bj.bcebos.com/hapi/darknet53.pdparams',
            '2506357a5c31e865785112fc614a487d')
}


class ConvBNLayer(fluid.dygraph.Layer):
    def __init__(self,
                 ch_in,
                 ch_out,
                 filter_size=3,
                 stride=1,
                 groups=1,
                 padding=0,
                 act="leaky"):
        super(ConvBNLayer, self).__init__()

        self.conv = Conv2D(
            num_channels=ch_in,
            num_filters=ch_out,
            filter_size=filter_size,
            stride=stride,
            padding=padding,
            groups=groups,
            param_attr=ParamAttr(
                initializer=fluid.initializer.Normal(0., 0.02)),
            bias_attr=False,
            act=None)
        self.batch_norm = BatchNorm(
            num_channels=ch_out,
            param_attr=ParamAttr(
                initializer=fluid.initializer.Normal(0., 0.02),
                regularizer=L2Decay(0.)),
            bias_attr=ParamAttr(
                initializer=fluid.initializer.Constant(0.0),
                regularizer=L2Decay(0.)))

        self.act = act

    def forward(self, inputs):
        out = self.conv(inputs)
        out = self.batch_norm(out)
        if self.act == 'leaky':
            out = fluid.layers.leaky_relu(x=out, alpha=0.1)
        return out

class DownSample(fluid.dygraph.Layer):
    def __init__(self,
                 ch_in,
                 ch_out,
                 filter_size=3,
                 stride=2,
                 padding=1):

        super(DownSample, self).__init__()

        self.conv_bn_layer = ConvBNLayer(
            ch_in=ch_in,
            ch_out=ch_out,
            filter_size=filter_size,
            stride=stride,
            padding=padding)
        self.ch_out = ch_out
    def forward(self, inputs):
        out = self.conv_bn_layer(inputs)
        return out

class BasicBlock(fluid.dygraph.Layer):
    def __init__(self, ch_in, ch_out):
        super(BasicBlock, self).__init__()

        self.conv1 = ConvBNLayer(
            ch_in=ch_in,
            ch_out=ch_out,
            filter_size=1,
            stride=1,
            padding=0)
        self.conv2 = ConvBNLayer(
            ch_in=ch_out,
            ch_out=ch_out*2,
            filter_size=3,
            stride=1,
            padding=1)
    def forward(self, inputs):
        conv1 = self.conv1(inputs)
        conv2 = self.conv2(conv1)
        out = fluid.layers.elementwise_add(x=inputs, y=conv2, act=None)
        return out

class LayerWarp(fluid.dygraph.Layer):
    def __init__(self, ch_in, ch_out, count):
        super(LayerWarp,self).__init__()

        self.basicblock0 = BasicBlock(ch_in, ch_out)
        self.res_out_list = []
        for i in range(1,count):
            res_out = self.add_sublayer("basic_block_%d" % (i),
                BasicBlock(
                    ch_out*2,
                    ch_out))
            self.res_out_list.append(res_out)
        self.ch_out = ch_out
    def forward(self,inputs):
        y = self.basicblock0(inputs)
        for basic_block_i in self.res_out_list:
            y = basic_block_i(y)
        return y


DarkNet_cfg = {53: ([1, 2, 8, 8, 4])}


class DarkNet(Model):
    """DarkNet model from
    `"YOLOv3: An Incremental Improvement" <https://arxiv.org/abs/1804.02767>`_

    Args:
        num_layers (int): layer number of DarkNet, only 53 supported currently, default: 53.
        ch_in (int): channel number of input data, default 3.
    """

    def __init__(self, num_layers=53, ch_in=3):
        super(DarkNet, self).__init__()
        assert num_layers in DarkNet_cfg.keys(), \
            "only support num_layers in {} currently" \
            .format(DarkNet_cfg.keys())
        self.stages = DarkNet_cfg[num_layers]
        self.stages = self.stages[0:5]

        self.conv0 = ConvBNLayer(
            ch_in=ch_in,
            ch_out=32,
            filter_size=3,
            stride=1,
            padding=1)

        self.downsample0 = DownSample(
            ch_in=32,
            ch_out=32 * 2)
        self.darknet53_conv_block_list = []
        self.downsample_list = []
        ch_in = [64,128,256,512,1024]
        for i, stage in enumerate(self.stages):
            conv_block = self.add_sublayer(
                "stage_%d" % (i),
                LayerWarp(
                int(ch_in[i]),
                32*(2**i),
                stage))
            self.darknet53_conv_block_list.append(conv_block)
        for i in range(len(self.stages) - 1):
            downsample = self.add_sublayer(
                "stage_%d_downsample" % i,
                DownSample(
                    ch_in = 32*(2**(i+1)),
                    ch_out = 32*(2**(i+2))))
            self.downsample_list.append(downsample)

    def forward(self,inputs):
        
        out = self.conv0(inputs)
        out = self.downsample0(out)
        blocks = []
        for i, conv_block_i in enumerate(self.darknet53_conv_block_list):
            out = conv_block_i(out)
            blocks.append(out)
            if i < len(self.stages) - 1:
                out = self.downsample_list[i](out)
        return blocks[-1:-4:-1]


def _darknet(num_layers=53, input_channels=3, pretrained=True):
    model = DarkNet(num_layers, input_channels)
    if pretrained:
        assert num_layers in pretrain_infos.keys(), \
                "DarkNet{} do not have pretrained weights now, " \
                "pretrained should be set as False".format(num_layers)
        weight_path = get_weights_path_from_url(*(pretrain_infos[num_layers]))
        assert weight_path.endswith('.pdparams'), \
                "suffix of weight must be .pdparams"
        model.load(weight_path[:-9])
    return model


def darknet53(input_channels=3, pretrained=True):
    """DarkNet 53-layer model
    
    Args:
        input_channels (bool): channel number of input data, default 3. 
        pretrained (bool): If True, returns a model pre-trained on ImageNet,
            default True.
    """
    return _darknet(53, input_channels, pretrained)
