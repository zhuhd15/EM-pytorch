import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import malis_core
# --------------------------------
# malis function: gt aff -> weight
# --------------------------------
# 1. building block
## level-0: utility layer
class malisWeight():
    def __init__(self, conn_dims, opt_weight=0.5, opt_nb=1):
        # pre-compute 
        self.opt_weight=opt_weight
        if opt_nb==1:
            self.nhood_data = malis_core.mknhood3d(1).astype(np.int32).flatten()
        else:
            self.nhood_data = malis_core.mknhood3d(1,1.8).astype(np.uint64).flatten()
        self.nhood_dims = np.array((3,3),dtype=np.uint64)
        self.num_vol = conn_dims[0]
        self.conn_dims = np.array(conn_dims[1:]).astype(np.uint64) # dim=4
        self.pre_ve, self.pre_prodDims, self.pre_nHood = malis_core.malis_init(self.conn_dims, self.nhood_data, self.nhood_dims)
        self.weight = np.zeros(conn_dims,dtype=np.float32)#pre-allocate

    def getWeight(self, x_cpu, aff_cpu, seg_cpu):
        for i in range(self.num_vol):
            self.weight[i] = malis_core.malis_loss_weights_both(seg_cpu[i].flatten(), self.conn_dims, self.nhood_data, self.nhood_dims, self.pre_ve, self.pre_prodDims, self.pre_nHood, x_cpu[i].flatten(), aff_cpu[i].flatten(), self.opt_weight).reshape(self.conn_dims)
        return self.weight


# for L2 training: re-weight the error by label bias (far more 1 than 0)
class labelWeight():
    def __init__(self, conn_dims, opt_weight=2, clip_low=0.01, clip_high=0.99, thres=0.5):
        self.opt_weight=opt_weight
        self.clip_low=clip_low
        self.clip_high=clip_high
        self.thres = thres
        self.num_vol = conn_dims[0]
        self.num_elem = np.prod(conn_dims[1:]).astype(float)
        self.weight = np.zeros(conn_dims,dtype=np.float32)#pre-allocate

    def getWeight(self, data):
        w_pos = self.opt_weight
        w_neg = 1.0-self.opt_weight
        for i in range(self.num_vol):
            if self.opt_weight==2:
                frac_pos = np.clip(data[i].mean(), self.clip_low, self.clip_high) #for binary labels
                # can't be all zero
                w_pos = 1.0/(2.0*frac_pos)
                w_neg = 1.0/(2.0*(1.0-frac_pos))
            self.weight[i] = np.add((data[i] >= self.thres) * w_pos, (data[i] < self.thres) * w_neg)
        return self.weight/self.num_elem

def mergeCrop(x1, x2):
    # x1 left, x2 right
    offset = [(x1.size()[x]-x2.size()[x])/2 for x in range(2,x1.dim()) for i in range(2)] 
    return torch.cat([x2, x1[:,:,offset[0]:offset[0]+x2.size(2),
            offset[1]:offset[1]+x2.size(3),offset[2]:offset[2]+x2.size(4)]], 1)
def mergeAdd(x1, x2):
    # x1 bigger
    offset = [(x1.size()[x]-x2.size()[x])/2 for x in range(1,x1.dim()) for i in range(2)] 
    #print x1.size(),x2.size(),offset
    return x2 + x1[:,offset[0]:offset[0]+x2.size(1),offset[1]:offset[1]+x2.size(2),
            offset[2]:offset[2]+x2.size(3),offset[3]:offset[3]+x2.size(4)]

def weightedMSE(input, target, weight=None, normalize_weight=False):
    # normalize by batchsize
    if weight is None:
        return torch.sum((input - target) ** 2)/input.size(0)
    else:
        if not normalize_weight:
            return torch.sum(weight * (input - target) ** 2)/input.size(0)
        else:
            return torch.sum(weight * (input - target) ** 2)/torch.numel(input)

# level-1: one conv-bn-relu-dropout neuron with different padding
class unitConv3dRBD(nn.Module):
    def __init__(self, in_num=1, out_num=1, kernel_size=1, stride_size=1, pad_size=0, pad_type='constant,0', has_bias=False, has_BN=False, relu_slope=-1, has_dropout=0):
        super(unitConv3dRBD, self).__init__()
        p_conv = pad_size if pad_type == 'constant,0' else 0 #  0-padding in conv layer 
        layers = [nn.Conv3d(in_num, out_num, kernel_size=kernel_size, padding=p_conv, stride=stride_size, bias=has_bias)] 
        if has_BN:
            layers.append(nn.BatchNorm3d(out_num))
        if relu_slope==0:
            layers.append(nn.ReLU(inplace=True))
        elif relu_slope>0:
            layers.append(nn.LeakyReLU(relu_slope))
        if has_dropout>0:
            layers.append(nn.Dropout3d(has_dropout, True))
        self.cbrd = nn.Sequential(*layers)
        self.pad_type = pad_type
        if pad_size==0:
            self.pad_size = 0
        else:
            self.pad_size = tuple([pad_size]*6)
        if ',' in pad_type:
            self.pad_value = float(pad_type[pad_type.find(',')+1:])

    def forward(self, x):
        if isinstance(self.pad_size,int) or self.pad_type == 'constant,0': # no padding or 0-padding
            return self.cbrd(x)
        else:
            if ',' in self.pad_type:# constant padding
                return self.cbrd(F.pad(x,self.pad_size,'constant',self.pad_value))
            else:
                if self.pad_type!='reflect':# reflect: hack with numpy (not implemented in)...
                    return self.cbrd(F.pad(x,self.pad_size,self.pad_type))
class unitResBottleneck(nn.Module):
    expansion = 4
    def __init__(self, in_num, out_num,  stride_size=1, downsample=None, 
                 cfg={'pad_size':1, 'pad_type':'constant,0', 'has_BN':True}):
        super(unitResBottleneck, self).__init__()
        self.conv = nn.Sequential(
            unitConv3dRBD(in_num, out_num, 1, has_BN=has_BN),
            unitConv3dRBD(out_num, out_num, 3, stride, pad_size, pad_type, has_BN=has_BN),
            unitConv3dRBD(out_num, out_num*4, 1, has_BN=has_BN))
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        residual = x
        out = self.convs(x)
        if self.downsample is not None:
            residual = self.downsample(x)
        out += residual
        out = self.relu(out)
        return out

class unitResBasic(nn.Module):
    expansion = 1
    def __init__(self, in_num, out_num, kernel_size=1, stride_size=1, do_sample=1, 
                 pad_size=1, pad_type='constant,0', has_BN=True, relu_slope=0):
        super(unitResBasic, self).__init__()
        self.sample = None
        if do_sample>=0: # 1=downsample, 0=same size:
            self.conv= nn.Sequential(
                unitConv3dRBD(in_num, out_num, kernel_size, stride_size, pad_size, pad_type, has_BN=has_BN, relu_slope=relu_slope),
                unitConv3dRBD(out_num, out_num, kernel_size, 1, pad_size, pad_type, has_BN=has_BN))
            if (in_num != out_num * self.expansion) or (isinstance(stride_size,int) and stride_size!=1) or (not isinstance(stride_size,int) and  max(stride_size) != 1): # downsample:
                self.sample = unitConv3dRBD(in_num, out_num * self.expansion, 1, stride_size, has_BN=has_BN)
        else: # -1: upsample
            assert in_num == out_num
            self.conv= nn.Sequential(
                nn.ConvTranspose3d(in_num, in_num, stride_size, stride_size, groups=in_num, bias=False),
                unitConv3dRBD(in_num, in_num, kernel_size, 1, pad_size, pad_type, has_BN=has_BN))
            self.sample = nn.ConvTranspose3d(in_num, in_num, stride_size, stride_size, groups=in_num, bias=False)
        self.relu = None
        if relu_slope==0:
            self.relu = nn.ReLU(inplace=True)
        elif relu_slope>0 and relu_slope<1:
            self.relu = nn.LeakyReLU(relu_slope)

    def forward(self, x):
        residual = x
        out = self.conv(x)
        if self.sample is not None:
            residual = self.sample(x)
        out = mergeAdd(residual, out)
        if self.relu is not None:
            out = self.relu(out)
        return out

# level-2: list of level-1 blocks
def blockResNet(unit, unit_num, in_num, out_num, kernel_size=3, stride_size=1, do_sample=0,
                cfg={'pad_size':1, 'pad_type':'constant,0', 'has_BN':True, 'relu_slope':0.005}):
    layers = []
    pre_pad_size=cfg['pad_size']
    pre_pad_type=cfg['pad_type']
    cfg['pad_size'] = (kernel_size-1)/2
    cfg['pad_type'] = 'replicate'
    for i in range(unit_num):
        if i==unit_num-1:
            cfg['pad_size'] = pre_pad_size
            cfg['pad_type'] = pre_pad_type
        layers.append(unit(in_num, out_num, kernel_size, stride_size, do_sample, 
                           cfg['pad_size'], cfg['pad_type'], cfg['has_BN'], cfg['relu_slope']))
        in_num = out_num * unit.expansion
    return nn.Sequential(*layers)

def blockVgg(unit_num, in_num, out_num, kernel_size=3, stride_size=1, 
        cfg={'pad_size':0, 'pad_type':'', 'has_bias':True, 'has_BN':False, 'relu_slope':0.005, 'has_dropout':0}):
        layers= [unitConv3dRBD(in_num, out_num, kernel_size, stride_size, 
                        cfg['pad_size'], cfg['pad_type'], cfg['has_bias'], cfg['has_BN'], cfg['relu_slope'], cfg['has_dropout'])]
        for i in range(unit_num-1):
            if i==unit_num-2: # no dropout in the last layer
                cfg['has_dropout'] = 0
            layers.append(unitConv3dRBD(out_num, out_num, kernel_size, stride_size, cfg['pad_size'], cfg['pad_type'], cfg['has_bias'], cfg['has_BN'], cfg['relu_slope'], cfg['has_dropout']))
        return nn.Sequential(*layers)

# level-3: down-up module
class unetDown(nn.Module): # type 
    def __init__(self, opt, in_num, out_num, 
                 cfg_down={'pool_kernel': (1,2,2), 'pool_stride': (1,2,2), 'out_num': 1},
        cfg_conv={'pad_size':0, 'pad_type':'', 'has_bias':True, 'has_BN':False, 'relu_slope':-1, 'has_dropout':0}, block_id=0):
        super(unetDown, self).__init__()
        self.opt = opt
        if opt[0]==0: # max-pool
            self.down = nn.MaxPool3d(cfg_down['pool_kernel'], cfg_down['pool_stride'])
        elif opt[0]==1: # resBasic
            self.down = blockResNet(unitResBasic, 1, in_num, cfg_down['out_num'], stride_size=cfg_down['pool_stride'], cfg=cfg_conv)

        if opt[1]==0: # vgg-3x3 
            self.conv = blockVgg(2, in_num, out_num, cfg=cfg_conv)
        elif opt[1]==1: # res-18
            self.conv = blockResNet(unitResBasic, 2, in_num, out_num, cfg=cfg_conv)
    def forward(self, x):
        x1 = self.conv(x)
        x2 = self.down(x1)
        return x1, x2

class unetUp(nn.Module):
    # in1: skip layer, in2: previous layer
    def __init__(self, opt, in_num, outUp_num, inLeft_num, outConv_num,
        cfg_up={'pool_kernel': (1,2,2), 'pool_stride': (1,2,2)},
        cfg_conv={'pad_size':0, 'pad_type':'', 'has_bias':True, 'has_BN':False, 'relu_slope':0.005, 'has_dropout':0}, block_id=0):
        super(unetUp, self).__init__()
        self.opt = opt
        if opt[0]==0: # upsample+conv
            self.up = nn.Sequential(nn.ConvTranspose3d(in_num, in_num, cfg_up['pool_kernel'], cfg_up['pool_stride'], groups=in_num, bias=False),
                unitConv3dRBD(in_num, outUp_num, 1, 1, 0, '', True))
            # not supported yet: anisotropic upsample
            # self.up = nn.Sequential(nn.Upsample(scale_factor=cfg_up.pool_kernel, mode='nearest'),
            #    unitConv3dRBD(in_num, outUp_num, 1, 1, 0, '', True))
        elif opt[0]==1: # group deconv, remember to initialize with (1,0)
            self.up = nn.ConvTranspose3d(in_num, in_num, cfg_up['pool_kernel'], cfg_up['pool_stride'], groups=in_num, bias=True)
            outUp_num = in_num
        elif opt[0]==2: # residual deconv
            self.up = blockResNet(unitResBasic, 1, in_num, in_num, stride_size=cfg_down['pool_stride'], do_sample=-1, cfg=cfg_conv)
            outUp_num = in_num

        if opt[1]==0: # merge-crop
            self.mc = mergeCrop
            inConv_num = outUp_num+inLeft_num
        elif opt[1]==1: # merge-add
            self.mc = mergeAdd
            inConv_num = min(outUp_num,inLeft_num)

        if opt[2]==0: # deconv
            self.conv = blockVgg(2, inConv_num, outConv_num, cfg=cfg_conv)
        elif opt[2]==1: # residual
            self.conv = blockResNet(unitResBasic, 2, inConv_num, outConv_num, cfg=cfg_conv)

    def forward(self, x1, x2):
        # inputs1 from left-side (bigger)
        x2_up = self.up(x2)
        mc = self.mc(x1, x2_up)
        return self.conv(mc)

class unetCenter(nn.Module):
    def __init__(self, opt, in_num, out_num,
        cfg_conv={'pad_size':0, 'pad_type':'', 'has_bias':True, 'has_BN':False, 'relu_slope':0.005, 'has_dropout':0} ):
        super(unetCenter, self).__init__()
        self.opt = opt
        if opt[0]==0: # vgg
            self.conv = blockVgg(2, in_num, out_num, cfg=cfg_conv)
        elif opt[0]==1: # residual
            self.conv = blockResNet(unitResBasic, 2, in_num, out_num, cfg=cfg_conv)
    def forward(self, x):
        return self.conv(x)

class unetFinal(nn.Module):
    def __init__(self, opt, in_num, out_num):
        super(unetFinal, self).__init__()
        self.opt = opt
        if opt[0]==0: # vgg
            self.conv = unitConv3dRBD(in_num, out_num, 1, 1, 0, '', True)
        elif opt[0]==1: # resnet
            self.conv = blockResNet(unitResBasic, 1, in_num, out_num, 1, cfg={'pad_size':0, 'pad_type':'constant,0', 'has_BN':False, 'relu_slope':-1})
    def forward(self, x):
        return F.sigmoid(self.conv(x))


class unet3D(nn.Module): # symmetric unet
    def __init__(self, opt_arch=[[0,0],[0],[0,0,0],[0]], in_num=1, out_num=3, filters=[24,72,216,648],
                 has_bias=True, has_BN=False,has_dropout=0,pad_size=0,pad_type='',relu_slope=0.005,
                 pool_kernel=(1,2,2), pool_stride=(1,2,2)):
        super(unet3D, self).__init__()
        cfg_conv={'has_bias':has_bias,'has_BN':has_BN, 'has_dropout':has_dropout, 'pad_size':pad_size, 'pad_type':pad_type, 'relu_slope':relu_slope}
        cfg_pool={'pool_kernel':pool_kernel, 'pool_stride':pool_stride}

        self.depth = len(filters)-1 
        filters_in = [in_num] + filters[:-1]
        self.down = nn.ModuleList([
                    unetDown(opt_arch[0], filters_in[x], filters_in[x+1], cfg_pool, cfg_conv, x) 
                    for x in range(len(filters)-1)]) 
        self.center = unetCenter(opt_arch[1], filters[-2], filters[-1], cfg_conv)
        self.up = nn.ModuleList([
                    unetUp(opt_arch[2], filters[x], filters[x-1], filters[x-1], filters[x-1], cfg_pool, cfg_conv, x)
                    for x in range(len(filters)-1,0,-1)])
        self.final = unetFinal(opt_arch[3], filters[0], out_num) 

    def forward(self, x):
        down_u = [None]*self.depth
        for i in range(self.depth):
            down_u[i], x = self.down[i](x)
        x = self.center(x)
        for i in range(self.depth):
             x = self.up[i](down_u[self.depth-1-i],x)
        return self.final(x)

# --------------------------------
# 2. utility function
def decay_lr(optimizer, base_lr, iter, policy='inv',gamma=0.0001, power=0.75, step=100): 
    if policy=='fixed':
        return
    elif policy=='inv':
        new_lr = base_lr * ((1+gamma*iter)**(-power)) 
    elif policy=='exp':
        new_lr = base_lr * (gamma**iter)
    elif policy=='step':
        new_lr = base_lr * (gamma**(np.floor(iter/step)))
    for group in optimizer.param_groups:
        if group['lr'] != 0.0: # not frozen
            group['lr'] = new_lr 

def save_checkpoint(model, filename='checkpoint.pth', optimizer=None, epoch=1):
    if optimizer is None:
        torch.save({
            'epoch': epoch,
            'state_dict': model.state_dict(),
        }, filename)
    else:
        torch.save({
            'epoch': epoch,
            'state_dict': model.state_dict(),
            'optimizer' : optimizer.state_dict()
        }, filename)

def load_checkpoint(snapshot, num_gpu):
    # take care of multi-single gpu conversion
    cp = torch.load(snapshot)
    if num_gpu==1 and cp['state_dict'].keys()[0][:7]=='module.':
        # modify the saved model for single GPU
        for k,v in cp['state_dict'].items():
            cp['state_dict'][k[7:]] = cp['state_dict'].pop(k,None)
    elif num_gpu>1 and (len(cp['state_dict'].keys()[0])<7 or cp['state_dict'].keys()[0][:7]!='module.'):
        # modify the single gpu model for multi-GPU
        for k,v in cp['state_dict'].items():
            cp['state_dict']['module.'+k] = v
            cp['state_dict'].pop(k,None)
    return cp

def weight_filler(ksizes, opt_scale=2.0, opt_norm=2):
    kk=0
    if opt_norm==0:# n_in
        kk = np.prod(ksizes[1:])
    elif opt_norm==1:# n_out
        kk = ksizes[0]*np.prod(ksizes[2:])
    elif opt_norm==2:# (n_in+n_out)/2
        kk = np.mean(ksizes[:2])*np.prod(ksizes[2:])
    # opt_scale: 1.0=xavier, 2.0=kaiming, 3.0=caffe
    ww = np.sqrt(opt_scale/float(kk))
    return ww

def init_weights(model,opt_init=0):
    opt=[[2.0,2],[3.0,0]][opt_init]
    for i in range(3):
        for j in range(2):
            ksz = model.down[i].conv.convs[j].cbrd._modules['0'].weight.size()
            model.down[i].conv.conv.convs[j].cbrd._modules['0'].weight.data.copy_(torch.from_numpy(np.random.normal(0, weight_filler(ksz,opt[0],opt[1]), ksz)))
            model.down[i].conv.convs[j].cbrd._modules['0'].bias.data.fill_(0.0)
    # center
    for j in range(2):
        ksz = model.center.convs[j].cbrd._modules['0'].weight.size()
        model.center.convs[j].cbrd._modules['0'].weight.data.copy_(torch.from_numpy(np.random.normal(0, weight_filler(ksz,opt[0],opt[1]), ksz)))
        model.center.convs[j].cbrd._modules['0'].bias.data.fill_(0.0)
    # up
    for i in range(3):
        model.up[i].up.weight.data.fill_(1.0)
        ksz = model.up[i].up_conv.convs[0].cbrd._modules['0'].weight.size()
        model.up[i].up_conv.convs[0].cbrd._modules['0'].weight.data.copy_(torch.from_numpy(np.random.normal(0, weight_filler(ksz,opt[0],opt[1]), ksz)))
        model.up[i].up_conv.convs[0].cbrd._modules['0'].bias.data.fill_(0.0)
        for j in range(2):
            ksz = model.up[i].conv.convs[j].cbrd._modules['0'].weight.size()
            model.up[i].conv.convs[j].cbrd._modules['0'].weight.data.copy_(torch.from_numpy(np.random.normal(0, weight_filler(ksz,opt[0],opt[1]), ksz)))
            model.up[i].conv.convs[j].cbrd._modules['0'].bias.data.fill_(0.0)
    ksz = model.final.conv.weight.size()
    model.final.convs[0].cbrd.weight.data.copy_(torch.from_numpy(np.random.normal(0, weight_filler(ksz,opt[0],opt[1]), ksz)))
    model.final.convs[0].cbrd.bias.data.fill_(0.0)

def load_weights_pkl(model, weights):
    # todo: add BN param
    # down
    for i in range(3):
        for j in range(2):
            ww = weights['Convolution'+str(2*i+j+1)]
            model.down[i].conv.convs[j].cbrd._modules['0'].weight.data.copy_(torch.from_numpy(ww['w']))
            model.down[i].conv.convs[j].cbrd._modules['0'].bias.data.copy_(torch.from_numpy(ww['b']))
    # center
    for j in range(2):
        ww = weights['Convolution'+str(7+j)]
        model.center.conv.convs[j].cbrd._modules['0'].weight.data.copy_(torch.from_numpy(ww['w']))
        model.center.conv.convs[j].cbrd._modules['0'].bias.data.copy_(torch.from_numpy(ww['b']))
    # up
    for i in range(3):
        ww = weights['Deconvolution'+str(i+1)]
        if model.up[i].opt[0]==0:# upsample+conv
            model.up[i].up._modules['0'].weight.data.copy_(torch.from_numpy(ww['w']))
            ww = weights['Convolution'+str(9+i*3)]
            model.up[i].up._modules['1'].cbrd._modules['0'].weight.data.copy_(torch.from_numpy(ww['w']))
            model.up[i].up._modules['1'].cbrd._modules['0'].bias.data.copy_(torch.from_numpy(ww['b']))
        elif model.up[i].opt[0]==1:# deconv
            model.up[i].up.weight.data.copy_(torch.from_numpy(ww['w']))
        for j in range(2):
            ww = weights['Convolution'+str(10+3*i+j)]
            model.up[i].conv.convs[j].cbrd._modules['0'].weight.data.copy_(torch.from_numpy(ww['w']))
            model.up[i].conv.convs[j].cbrd._modules['0'].bias.data.copy_(torch.from_numpy(ww['b']))
    ww = weights['Convolution18']
    model.final.conv.cbrd._modules['0'].weight.data.copy_(torch.from_numpy(ww['w']))
    model.final.conv.cbrd._modules['0'].bias.data.copy_(torch.from_numpy(ww['b']))
