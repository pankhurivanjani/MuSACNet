import math

import torch
import torch.nn as nn
import torchvision
from torchsummary import summary

def weights_init(m):
    # Initialize filters with Gaussian random weights
    if isinstance(m, nn.Conv2d):
        n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
        m.weight.data.normal_(0, math.sqrt(2. / n))
        if m.bias is not None:
            m.bias.data.zero_()
    elif isinstance(m, nn.ConvTranspose2d):
        n = m.kernel_size[0] * m.kernel_size[1] * m.in_channels
        m.weight.data.normal_(0, math.sqrt(2. / n))
        if m.bias is not None:
            m.bias.data.zero_()
    elif isinstance(m, nn.BatchNorm2d):
        m.weight.data.fill_(1)
        m.bias.data.zero_()


class MeanFieldUpdate(nn.Module):
    """
    Meanfield updating for the features and the attention for one pair of features.
    bottom_list is a list of observation features derived from the backbone CNN.

    update attention map
    a_s <-- y_s * (K_s conv y_S)
    a_s = b_s conv a_s
    a_s <-- Sigmoid(-(a_s + a_s))

    update the last scale feature map y_S
    y_s <-- K conv y_s
    y_S <-- x_S + (a_s * y_s)
    """

    def __init__(self, bottom_send, bottom_receive, feat_num):
        super(MeanFieldUpdate, self).__init__()

        self.atten_f = nn.Conv2d(in_channels=bottom_send + bottom_receive, out_channels=feat_num,
                                 kernel_size=3, stride=1, padding=1)
        self.norm_atten_f = nn.Sigmoid()
        self.message_f = nn.Conv2d(in_channels=bottom_send, out_channels=feat_num, kernel_size=3,
                                   stride=1, padding=1)
        self.Scale = nn.Conv2d(in_channels=feat_num, out_channels=bottom_receive, kernel_size=1, bias=True)

    def forward(self, x_s, x_S):
        # update attention map
        a_s = torch.cat((x_s, x_S), dim=1)
        a_s = self.atten_f(a_s)
        a_s = self.norm_atten_f(a_s)

        # update the last scale feature map y_S
        y_s = self.message_f(x_s)
        y_S = y_s.mul(a_s)  # production
        # scale
        y_S = self.Scale(y_S)
        y_S = x_S + y_S  # eltwise sum
        return y_S


class back(nn.Module):
    '''
        Reproduce the size of image
    '''

    def __init__(self, feat_num):
        super().__init__()
        # produce the output
        self.pred_1 = nn.ConvTranspose2d(feat_num, feat_num // 2, kernel_size=4, stride=2, padding=1) # op - 256
        self.bn1 = nn.BatchNorm2d(feat_num // 2)
        # self.pred_1_relu = nn.ReLU(inplace=True)
        self.pred_1_relu = nn.SELU(inplace=True)
        self.pred_2 = nn.ConvTranspose2d(feat_num // 2, feat_num // 4, kernel_size=4, stride=2, padding=1) # op -128
        self.bn2 = nn.BatchNorm2d(feat_num // 4)
        self.pred_2_relu = nn.SELU(inplace=True)
        self.pred_3 = nn.Conv2d(feat_num // 4, feat_num // 8, kernel_size=3, stride=1, padding=1) #op - 1 
        self.bn3 = nn.BatchNorm2d(feat_num // 8)
        self.pred_3_relu = nn.SELU(inplace=True)
        self.pred_4 = nn.Conv2d(feat_num // 8, 1, kernel_size=3, stride=1, padding=1) #op - 1 
        

    def forward(self, x):
        # y_S = res5c # skipping mean field update; res5c is last output feature map
        pred = self.pred_1(x) # from feat_num to 1 channels
        pred = self.bn1(pred) # extra
        pred = self.pred_1_relu(pred) # SELU
        pred = self.pred_2(pred)
        pred = self.bn2(pred) # extra
        pred = self.pred_2_relu(pred) # SELU
        pred = self.pred_3(pred)
        pred = self.pred_3_relu(self.bn3(pred))
        pred = self.pred_4(pred)

        # print(pred.size())
        if self.training:
            pred = nn.functional.interpolate(pred, size=(256, 1024), mode='bilinear', align_corners=True) # 256, 512
        else:
            pred = nn.functional.interpolate(pred, size=(368, 1232), mode='bilinear', align_corners=True)

        return pred


class front(nn.Module):
    """
    Based on ResNet-50
    Perform feature selection with MFU (based on CRF)
    """

    def __init__(self, in_channels=3, feat_num=512, feat_width=80, feat_height=24, pretrained=True):
        # super(SAN, self).__init__()
        super().__init__()
        # backbone Net: ResNet
        pretrained_model = torchvision.models.__dict__['resnet{}'.format(34)](pretrained=pretrained)
        self.channel = in_channels

        self.conv1 = pretrained_model._modules['conv1']
        self.bn1 = pretrained_model._modules['bn1']
        self.relu = pretrained_model._modules['relu']

        self.maxpool = pretrained_model._modules['maxpool']
        self.layer1 = pretrained_model._modules['layer1']
        self.layer2 = pretrained_model._modules['layer2']
        self.layer3 = pretrained_model._modules['layer3']
        self.layer4 = pretrained_model._modules['layer4']

        # generating multi-scale features with the same dimension
        # in paper,  type = 'gaussian'
        self.res4f_dec_1 = nn.ConvTranspose2d(256, feat_num, kernel_size=4, stride=2, padding=1) # 1024 last val
        # self.res4f_dec_1_relu = nn.ReLU(inplace=True)
        self.res4f_dec_1_relu = nn.ReLU(inplace=True)

        # in paper,  type = 'gaussian'
        self.res5c_dec_1 = nn.ConvTranspose2d(512, feat_num, kernel_size=8, stride=4, padding=2) # 2048 last val - resnet50
        # self.res5c_dec_1_relu = nn.ReLU(inplace=True)
        self.res5c_dec_1_relu = nn.ReLU(inplace=True)

        self.res4f_dec = nn.UpsamplingBilinear2d(size=(feat_height, feat_width))
        self.res3d_dec = nn.UpsamplingBilinear2d(size=(feat_height, feat_width))
        self.res5c_dec = nn.UpsamplingBilinear2d(size=(feat_height, feat_width))

        # add deep supervision for three semantic layers
        # self.prediction_3d = nn.Conv2d(feat_num, out_channels=1, kernel_size=3, stride=1, padding=1)
        # self.prediction_4f = nn.Conv2d(feat_num, out_channels=1, kernel_size=3, stride=1, padding=1)
        # self.prediction_5c = nn.Conv2d(feat_num, out_channels=1, kernel_size=3, stride=1, padding=1)

        # # # the first meanfield updating
        self.meanFieldUpdate1_1 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate1_2 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate1_3 = MeanFieldUpdate(feat_num, feat_num, feat_num)

        # the second meanfield updating
        self.meanFieldUpdate2_1 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate2_2 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate2_3 = MeanFieldUpdate(feat_num, feat_num, feat_num)

        # the third meanfield updating
        self.meanFieldUpdate3_1 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate3_2 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate3_3 = MeanFieldUpdate(feat_num, feat_num, feat_num)

        # the fourth meanfield updating
        self.meanFieldUpdate4_1 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate4_2 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate4_3 = MeanFieldUpdate(feat_num, feat_num, feat_num)

        # the fifth meanfield updating
        self.meanFieldUpdate5_1 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate5_2 = MeanFieldUpdate(feat_num, feat_num, feat_num)
        self.meanFieldUpdate5_3 = MeanFieldUpdate(feat_num, feat_num, feat_num)

        # weights init
        self.res4f_dec_1.apply(weights_init)
        self.res5c_dec_1.apply(weights_init)
        # self.prediction_3d.apply(weights_init)
        # self.prediction_4f.apply(weights_init)
        # self.prediction_5c.apply(weights_init)

        self.meanFieldUpdate1_1.apply(weights_init)
        self.meanFieldUpdate1_2.apply(weights_init)
        self.meanFieldUpdate1_3.apply(weights_init)

        self.meanFieldUpdate2_1.apply(weights_init)
        self.meanFieldUpdate2_2.apply(weights_init)
        self.meanFieldUpdate2_3.apply(weights_init)

        self.meanFieldUpdate3_1.apply(weights_init)
        self.meanFieldUpdate3_2.apply(weights_init)
        self.meanFieldUpdate3_3.apply(weights_init)

        self.meanFieldUpdate4_1.apply(weights_init)
        self.meanFieldUpdate4_2.apply(weights_init)
        self.meanFieldUpdate4_3.apply(weights_init)

        self.meanFieldUpdate5_1.apply(weights_init)
        self.meanFieldUpdate5_2.apply(weights_init)
        self.meanFieldUpdate5_3.apply(weights_init)

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        x = self.layer1(x)
        x = self.layer2(x)
        res3d = x
        x = self.layer3(x)
        res4f = x
        x = self.layer4(x)
        res5c = x

        # generate multi-scale features with the same dimension,
        res4f_1 = self.res4f_dec_1(res4f)  # 1024 --> 512
        res4f_1 = self.res4f_dec_1_relu(res4f_1)

        res5c_1 = self.res5c_dec_1(res5c)  # 1024 --> 512
        res4f_1 = self.res5c_dec_1_relu(res5c_1)

        res4f = self.res4f_dec(res4f_1)
        res3d = self.res3d_dec(res3d)
        res5c = self.res5c_dec(res5c_1)

        # pred_3d = self.prediction_3d(res3d)
        # pred_4f = self.prediction_4f(res4f)
        # pred_5c = self.prediction_5c(res5c)

        # # five meanfield updating
        y_S = self.meanFieldUpdate1_1(res3d, res5c)
        y_S = self.meanFieldUpdate1_2(res4f, y_S)
        y_S = self.meanFieldUpdate1_3(res5c, y_S)

        y_S = self.meanFieldUpdate2_1(res3d, y_S)
        y_S = self.meanFieldUpdate2_2(res4f, y_S)
        y_S = self.meanFieldUpdate2_3(res5c, y_S)

        y_S = self.meanFieldUpdate3_1(res3d, y_S)
        y_S = self.meanFieldUpdate3_2(res4f, y_S)
        y_S = self.meanFieldUpdate3_3(res5c, y_S)

        y_S = self.meanFieldUpdate4_1(res3d, y_S)
        y_S = self.meanFieldUpdate4_2(res4f, y_S)
        y_S = self.meanFieldUpdate4_3(res5c, y_S)

        y_S = self.meanFieldUpdate5_1(res3d, y_S)
        y_S = self.meanFieldUpdate5_2(res4f, y_S)
        y_S = self.meanFieldUpdate5_3(res5c, y_S)
        # y_S = res5c

        return y_S

class SAStereonet(nn.Module):
    """ 
        Our network for stereo matching
    """

    def __init__(self, in_channels=3, feat_num=512, feat_width=80, feat_height=24, pretrained=True):
        super().__init__()
        self.feature_extractor = front(in_channels, feat_num, feat_width, feat_height, pretrained)
        self.deconv = back(2*feat_num) # double number of channel layers

    def forward(self, leftimg, rightimg):
        feat_right = self.feature_extractor(rightimg)
        feat_left = self.feature_extractor(leftimg)
        
        final_feat = torch.cat((feat_left, feat_right), 1) # concatenate along feature layers dim

        pred = self.deconv(final_feat)

        return pred
