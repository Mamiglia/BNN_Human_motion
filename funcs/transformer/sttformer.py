from os import pidfd_open
import torch.nn as nn
from typing import Tuple
from .sta_block import STA_Block
from bayesian_torch.layers import LinearReparameterization as LinearBayes, Conv1dReparameterization as Conv1dBayes
import torch

def conv_init(conv):
    nn.init.kaiming_normal_(conv.weight, mode='fan_out')
    # nn.init.constant_(conv.bias, 0)

def bn_init(bn, scale):
    nn.init.constant_(bn.weight, scale)
    nn.init.constant_(bn.bias, 0)

def fc_init(fc):
    nn.init.xavier_normal_(fc.weight)
    nn.init.constant_(fc.bias, 0)


class STTFormerBayes(nn.Module):
    def __init__(self, num_joints, 
                 num_frames, num_frames_out, num_heads, num_channels, 
                 kernel_size, len_parts=1, use_pes=True, config=None, num_persons=1,
                 att_drop=0, dropout=0, dropout2d=0, repetitions=100):
        super().__init__()

        self.reps = repetitions

        config = [
            [16,16,16], [16,16,16], 
            [16,16,16], [16,16,16],
            [16,16,16], [16,16,16], 
            [16,16,16], [16, 3,16]]

        self.num_frames = num_frames
        self.num_frames_out = num_frames_out
        self.num_joints = num_joints
        self.num_channels = num_channels
        self.num_persons = num_persons
        self.len_parts = len_parts
        in_channels = config[0][0]
        self.out_channels = config[-1][1]

        num_frames = num_frames // len_parts
        num_joints = num_joints * len_parts
        
        self.input_map = nn.Sequential(
            nn.Conv2d(num_channels, in_channels, 1),
            nn.BatchNorm2d(in_channels),
            nn.LeakyReLU(0.1))

        self.blocks = nn.ModuleList()
        for _, (in_channels, out_channels, qkv_dim) in enumerate(config):
            self.blocks.append(STA_Block(in_channels, out_channels, qkv_dim, 
                                         num_frames=num_frames, 
                                         num_joints=num_joints, 
                                         num_heads=num_heads,
                                         kernel_size=kernel_size,
                                         use_pes=use_pes,
                                         att_drop=att_drop))   
            
        # REPLACED WITH BNN 
        self.conv_out = Conv1dBayes(num_frames, num_frames_out, 1, stride=1)
        self.fc_out = LinearBayes(66, 66)
        
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                conv_init(m)
            elif isinstance(m, nn.BatchNorm2d):
                bn_init(m, 1)
            elif isinstance(m, nn.Linear):
                fc_init(m)

    def stochastic(self, x):
        x, kl1 = self.conv_out(x)
        x, kl2 = self.fc_out(x)  
        kl_sum = kl1 + kl2

        return x, kl_sum

    def forward(self, x : torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]: 
        x = x.reshape(-1, self.num_frames, self.num_joints, self.num_channels, self.num_persons).permute(0, 3, 1, 2, 4).contiguous()
        N, C, T, V, M = x.shape
        
        x = x.permute(0, 4, 1, 2, 3).contiguous().view(N * M, C, T, V)
        
        x = x.view(x.size(0), x.size(1), T // self.len_parts, V * self.len_parts)
        x = self.input_map(x)
        
        for _, block in enumerate(self.blocks):
            x = block(x)

        x = x.reshape(-1, self.num_frames, self.num_joints*self.num_channels)
        
        x_kl = [self.stochastic(x) for _ in range(self.reps)]
        x_pop, kl_pop = zip(*x_kl)
        kl = sum(kl for kl in kl_pop)
        x_pop = torch.stack(x_pop).view(self.reps, -1, self.num_frames_out, self.num_joints, 3)
        return x_pop, kl / self.reps

