import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import build_norm_layer
import scipy.io as sio
import mmcv
import os
from ..builder import HEADS
from .decode_head import BaseDecodeHead
from torchvision.utils import save_image, make_grid
from mmcv.runner import BaseModule, auto_fp16, force_fp32
from ..utils.dalle_d_vae import get_dalle_vae, map_pixels, unmap_pixels
import torch
from mmcv.cnn import ConvModule
from ..losses import accuracy
from mmseg.models.builder import HEADS
from mmseg.models.decode_heads.decode_head import BaseDecodeHead
from mmseg.ops import resize

@HEADS.register_module()
class BigSegAggHeadRelaxE08HungerDepth(BaseDecodeHead):
    """
    Args:
        norm_layer (dict): Config dict for input normalization.
            Default: norm_layer=dict(type='LN', eps=1e-6, requires_grad=True).
        num_convs (int): Number of decoder convolutions. Default: 1.
        up_scale (int): The scale factor of interpolate. Default:4.
        kernel_size (int): The kernel size of convolution when decoding
            feature information from backbone. Default: 3.
        init_cfg (dict | list[dict] | None): Initialization config dict.
            Default: dict(
                     type='Constant', val=1.0, bias=0, layer='LayerNorm').
    """

    def __init__(self,
                 channels,
                 img_size,
                 init_cfg=[
                     dict(type='Constant', val=1.0, bias=0, layer='LayerNorm'),
                     dict(
                         type='Normal',
                         std=0.01,
                         override=dict(name='conv_seg'))],
                 norm_layer=dict(type='LN', eps=1e-6, requires_grad=True),
                 indice_channel_index=0,
                 pixel_channel_index=1,
                 interpolate_mode='bilinear',
                 palette=None,
                 indice_seg_channel=None,
                 framework='mmseg',
                 **kwargs):
        super(BigSegAggHeadRelaxE08HungerDepth, self).__init__(init_cfg=init_cfg, input_transform='multiple_select', channels=channels, **kwargs)
        num_inputs = len(self.in_channels)
        assert num_inputs == len(self.in_index)
        self.channels = channels
        self.palette = torch.tensor(palette)
        self.interpolate_mode = interpolate_mode
        self.img_size = img_size
        self.vocab_size = 8192
        self.indice_ignore_index = self.vocab_size
        self.pixel_ignore_index = 256
        _, self.norm = build_norm_layer(norm_layer, self.channels)
        self.pixel_channel_index = pixel_channel_index
        self.indice_channel_index = indice_channel_index
        self.indice_seg_channel = indice_seg_channel if indice_seg_channel is not None else channels
        # dense classificator
        self.conv_seg = nn.Conv2d(self.indice_seg_channel, self.vocab_size, kernel_size=1)
        self.conv_seg_pixel = nn.Conv2d(channels, 1, kernel_size=1)
        self.total_num = 0
        self.sm = 0
        self.max_depth = 10
        self.min_depth = 1e-3
        # input translation
        self.convs = nn.ModuleList()
        self.convs_pixel = nn.ModuleList()
        for i in range(num_inputs):
            self.convs.append(
                ConvModule(
                    in_channels=self.in_channels[i],
                    out_channels=self.channels,
                    kernel_size=1,
                    stride=1,
                    norm_cfg=self.norm_cfg,
                    act_cfg=self.act_cfg))
            self.convs_pixel.append(
                ConvModule(
                    in_channels=self.in_channels[i],
                    out_channels=self.channels,
                    kernel_size=1,
                    stride=1,
                    norm_cfg=self.norm_cfg,
                    act_cfg=self.act_cfg))

        # fusion blocks
        self.fusion_conv = ConvModule(
            in_channels=self.channels * num_inputs,
            out_channels=self.indice_seg_channel,
            kernel_size=1,
            norm_cfg=self.norm_cfg)
        self.fusion_conv_pixel = ConvModule(
            in_channels=self.channels * num_inputs,
            out_channels=self.channels,
            kernel_size=1,
            norm_cfg=self.norm_cfg)

        self.d_vae = get_dalle_vae(
            weight_path="/home/chenjiaqi/pj/mmsegmentation/ckp",
            device="cuda")

        self.criterion = nn.MSELoss(reduction="mean")

    def forward(self, x):
        out = self.cls_seg(x)
        return out

    def get_gt_vq_indices(self, gt_semantic_seg):
        gt_segmap = map_pixels(self.encode_to_segmap(gt_semantic_seg) / 255.0)
        return self.d_vae.get_codebook_indices(gt_segmap)

    def feature_aggregation(self, inputs):
        outs = []
        for idx in range(len(inputs)):
            x = inputs[idx]
            conv = self.convs[idx]
            outs.append(
                resize(
                    input=conv(x),
                    size=inputs[self.indice_channel_index].shape[2:],
                    mode=self.interpolate_mode,
                    align_corners=self.align_corners))

        out = self.fusion_conv(torch.cat(outs, dim=1))
        return out

    def feature_aggregation_for_pixel(self, inputs):
        outs_for_pixel = []
        for idx in range(len(inputs)):
            x = inputs[idx]
            conv = self.convs_pixel[idx]
            outs_for_pixel.append(
                resize(
                    input=conv(x),
                    size=inputs[self.pixel_channel_index].shape[2:],
                    mode=self.interpolate_mode,
                    align_corners=self.align_corners))
        out_for_pixel = self.fusion_conv_pixel(torch.cat(outs_for_pixel, dim=1))
        return out_for_pixel

    def forward_train(self, inputs, img_metas, gt_semantic_seg, train_cfg):
        print('cjq debug ', gt_semantic_seg)
        inputs = self._transform_inputs(inputs)
        x = self.feature_aggregation(inputs)
        x_p = self.feature_aggregation_for_pixel(inputs)
        h, w = x.shape[-2:]
        h_p, w_p = x_p.shape[-2:]
        vq_logits = self.forward(x).view(-1, self.vocab_size, h, w)
        pixel_logits = self.conv_seg_pixel(x_p).view(-1, 1, h_p, w_p)
        pixel_logits = F.interpolate(pixel_logits, size=(h * 8, w * 8), mode='bilinear')
        pixel_depth = F.sigmoid(pixel_logits) * self.max_depth
        # get vq indices from gt by dalle
        with torch.no_grad():
            # interpolate only preform when 1/4 scale was used
            gt_semantic_seg[gt_semantic_seg == self.pixel_ignore_index] = self.num_classes
            gt_semantic_seg = F.interpolate(
                F.one_hot(gt_semantic_seg.to(torch.long), self.num_classes + 1).squeeze(1).permute(0, 3, 1, 2).to(
                    torch.float),
                size=(h * 8, w * 8), mode='bilinear').argmax(1).unsqueeze(1)
            gt_semantic_seg[gt_semantic_seg == self.num_classes] = self.pixel_ignore_index

            # get non-ignore gt_indice
            pixel_pred = pixel_logits.argmax(1).unsqueeze(1)
            gt_semantic_seg_for_recon = torch.zeros_like(gt_semantic_seg)
            gt_semantic_seg_for_recon[gt_semantic_seg != self.pixel_ignore_index] = gt_semantic_seg[gt_semantic_seg != self.pixel_ignore_index].clone()
            gt_semantic_seg_for_recon[gt_semantic_seg == self.pixel_ignore_index] = pixel_pred[gt_semantic_seg == self.pixel_ignore_index].clone()
            gt_semantic_seg_indices = self.get_gt_vq_indices(gt_semantic_seg_for_recon).unsqueeze(1) # % 100

            # get the pixel-wise prediction from indice prediction
            # pixel_segmap_from_indice_pred = self.d_vae.decode(vq_logits.argmax(1).unsqueeze(1), img_size=[h, w])
            # pixel_segmap_from_indice_pred = unmap_pixels(torch.sigmoid(pixel_segmap_from_indice_pred[:, :3])) * 255
            # pixel_pred_from_indice_pred = self.decode_from_segmap(torch.tensor(pixel_segmap_from_indice_pred), keep_ignore_index=False)

            # # get relaxed correct pixel mask
            # relaxation_map = torch.ones_like(gt_semantic_seg, device=gt_semantic_seg.device)
            # relaxation_map[pixel_pred_from_indice_pred == gt_semantic_seg] = 0
            # relaxation_map = F.avg_pool2d(relaxation_map.float(), kernel_size=(32, 32), stride=(8, 8))
            # relaxation_map = F.interpolate(relaxation_map.float(), size=(h, w), mode='bilinear')
            # relaxation_map[relaxation_map >= 0.0001] = 1
            # relaxation_map[relaxation_map < 1] = 0

            # get ignore mask
            ignore_map = torch.ones_like(gt_semantic_seg, device=gt_semantic_seg.device)
            ignore_map[gt_semantic_seg >= self.num_classes] = 0
            ignore_mask = F.max_pool2d(ignore_map.float(), kernel_size=(8, 8), stride=(8, 8))
            indice_map_mask = ignore_mask

            # get final gt indices
            masked_gt_semantic_seg_indices = gt_semantic_seg_indices.clone()
            masked_gt_semantic_seg_indices[indice_map_mask == 0] = self.indice_ignore_index

            # 10.16 32号上的第一个bigseg实验没有ignore，而是按照teacher student的范式监督了student
            gt_semantic_seg_indices[ignore_mask == 0] = self.indice_ignore_index

        losses = self.losses(
            indice_logit=vq_logits,
            pixel_depth_logit=pixel_depth,
            indice_label=masked_gt_semantic_seg_indices,
            pixel_depth_label=gt_semantic_seg)

        return losses

    def forward_test(self, inputs, img_metas, gt_semantic_seg, test_cfg):
        return self._forward_depth(inputs, img_metas, gt_semantic_seg, test_cfg)
        # return self._forward_test_recon_with_dalle(gt_semantic_seg, img_metas)
        inputs = self._transform_inputs(inputs)
        x = self.feature_aggregation(inputs)
        h, w = x.shape[-2:]
        vq_logist = self.forward(x).view(-1, self.vocab_size, h, w)
        h, w = vq_logist.shape[-2:]
        vq_indices = vq_logist.argmax(1).unsqueeze(1)
        rec_segmap = self.d_vae.decode(vq_indices, img_size=[h, w])
        rec_segmap = unmap_pixels(torch.sigmoid(rec_segmap[:, :3])) * 255
        seg_pred = self.decode_from_segmap(torch.tensor(rec_segmap), keep_ignore_index=False)
        # seg_pred[seg_pred == self.num_classes] = 0  # b, h, w, c
        seg_logist = F.one_hot(seg_pred.to(torch.int64), self.num_classes).squeeze(1).permute(0, 3, 1, 2).to(
            torch.float)

        return seg_logist  # [:,:-1,:,:]


    def _forward_depth(self, inputs, img_metas, gt_semantic_seg, test_cfg):
        inputs = self._transform_inputs(inputs)
        x = self.feature_aggregation(inputs)
        h, w = x.shape[-2:]
        vq_logist = self.forward(x).view(-1, self.vocab_size, h, w)
        h, w = vq_logist.shape[-2:]
        vq_indices = vq_logist.argmax(1).unsqueeze(1)
        rec_segmap = self.d_vae.decode(vq_indices, img_size=[h, w])
        rec_segmap = unmap_pixels(torch.sigmoid(rec_segmap[:, :3])) * 255
        seg_pred = self.decode_from_segmap(torch.tensor(rec_segmap), keep_ignore_index=False)
        # seg_pred[seg_pred == self.num_classes] = 0  # b, h, w, c
        seg_logist = F.one_hot(seg_pred.to(torch.int64), self.num_classes).squeeze(1).permute(0, 3, 1, 2).to(
            torch.float)

        seg_pred_show = F.interpolate(rec_segmap.float(), size=gt_semantic_seg[0].shape[-2:], mode='bilinear')
        RMSELoss = torch.sqrt(self.criterion(seg_pred_show.mean(1) / 255.0, gt_semantic_seg[0] / 255.0))
        print('depth pred rmse:', RMSELoss)
        # save_image(gt_semantic_seg_item.float() / 255.0, 'work_dirs/nyu_depth_recon/show_gt/gt_val_' + img_metas[0]['ori_filename'].split('/')[-1])
        # save_image(torch.cat([gt_semantic_seg[0].unsqueeze(0) / 255.0,
        #                       seg_pred_show.mean(1).unsqueeze(0) / 255.0],
        #                      dim=0), 'work_dirs/nyu_depth_pred/show_final_pred/val_' + img_metas[0]['ori_filename'].split('/')[-1])
        # print('cjq save images')
        return seg_logist
    def _forward_test_recon_with_dalle(self, gt_semantic_seg, img_metas):

        assert isinstance(gt_semantic_seg, list)
        results = []
        for gt_semantic_seg_item in gt_semantic_seg:
            input_segmap = map_pixels(self.encode_to_segmap(gt_semantic_seg_item) / 255.0)
            input_ids = self.d_vae.get_codebook_indices(input_segmap)
            h, w = input_ids.shape[-2:]
            rec_segmap = self.d_vae.decode(input_ids, img_size=[h, w])
            rec_segmap = unmap_pixels(torch.sigmoid(rec_segmap[:, :3])) * 255
            seg_indices = self.decode_from_segmap(rec_segmap, keep_ignore_index=False)
            seg_logist = F.one_hot(seg_indices.to(torch.int64), self.num_classes).squeeze(1).permute(0, 3, 1, 2).to(
                torch.float)
            seg_logist = F.interpolate(seg_logist, size=gt_semantic_seg_item.shape[-2:], mode='bilinear')
            results.append(seg_logist) # [:,:self.num_classes,:,:]

            rec_segmap_show = F.interpolate(rec_segmap.float(), size=input_segmap.shape[-2:], mode='bilinear')
            RMSELoss = torch.sqrt(self.criterion(rec_segmap_show / 255.0, input_segmap))
            self.total_num += 1
            self.sm += RMSELoss

            print('recon rmse:', self.sm / self.total_num)
            # save_image(gt_semantic_seg_item.float() / 255.0, 'work_dirs/nyu_depth_recon/show_gt/gt_val_' + img_metas[0]['ori_filename'].split('/')[-1])
            # save_image(torch.cat([input_segmap,
            #                       rec_segmap_show / 255.0],
            #                      dim=0), 'work_dirs/nyu_depth_recon/show/val_' + img_metas[0]['ori_filename'].split('/')[-1])
            # print('cjq save images')
        return torch.cat(results, dim=0)

    def encode_to_segmap(self, indice):
        PALETTE_ = self.palette.clone().to(indice.device)
        _indice = indice.clone().detach()
        _indice[_indice > self.num_classes] = self.num_classes
        return PALETTE_[_indice.long()].squeeze(1).permute(0, 3, 1, 2)

    def decode_from_segmap(self, segmap, keep_ignore_index):
        PALETTE_ = self.palette.clone().to(segmap.device) \
            if keep_ignore_index \
            else self.palette[:-1].clone().to(segmap.device)
        B, C, H, W = segmap.shape
        p = torch.Tensor.repeat(PALETTE_, B, H, W, 1, 1).permute(0, 3, 4, 1, 2)
        if keep_ignore_index:
            segmap = torch.Tensor.repeat(segmap, self.num_classes + 1, 1, 1, 1, 1).permute(1, 0, 2, 3, 4)
        else:
            segmap = torch.Tensor.repeat(segmap, self.num_classes, 1, 1, 1, 1).permute(1, 0, 2, 3, 4)
        return torch.abs(segmap - p).sum(2).argmin(1).unsqueeze(1)
        # return ((segmap - p) ** 2).sum(2).argmin(1).unsqueeze(1)

    @force_fp32(apply_to=('seg_logit', ))
    def losses(self,
               indice_logit,
               pixel_depth_logit,
               indice_label,
               pixel_depth_label):
        """Compute segmentation loss."""
        loss = dict()
        indice_logit = resize(
            input=indice_logit,
            size=indice_label.shape[2:],
            mode='bilinear',
            align_corners=self.align_corners)
        pixel_logit = resize(
            input=pixel_depth_logit,
            size=pixel_depth_label.shape[2:],
            mode='bilinear',
            align_corners=self.align_corners)
        if self.sampler is not None:
            indice_weight = self.sampler.sample(indice_logit, indice_label)
            pixel_weight = self.sampler.sample(pixel_logit, pixel_depth_label)
        else:
            indice_weight = None
            pixel_weight = None
        masked_indice_seg_label = indice_label.squeeze(1)
        # pixel_depth_label = pixel_depth_label.squeeze(1)
        if not isinstance(self.loss_decode, nn.ModuleList):
            losses_decode = [self.loss_decode]
        else:
            losses_decode = self.loss_decode
        for loss_decode in losses_decode:
            if loss_decode.loss_name not in loss:
                if loss_decode.loss_name == 'loss_ce':
                    loss[loss_decode.loss_name] = loss_decode(
                        indice_logit,
                        masked_indice_seg_label,
                        weight=indice_weight,
                        ignore_index=self.indice_ignore_index)
                elif loss_decode.loss_name == 'loss_mse_pixel':
                    loss[loss_decode.loss_name] = loss_decode(
                        pixel_logit,
                        pixel_depth_label,
                        weight=pixel_weight)
            else:
                if loss_decode.loss_name == 'loss_ce':
                    loss[loss_decode.loss_name] += loss_decode(
                        indice_logit,
                        masked_indice_seg_label,
                        weight=indice_weight,
                        ignore_index=self.indice_ignore_index)
                elif loss_decode.loss_name == 'loss_mse_pixel':
                    loss[loss_decode.loss_name] += loss_decode(
                        pixel_logit,
                        pixel_depth_label,
                        weight=pixel_weight)

        loss['rmse_aux'] = torch.sqrt(F.mse_loss(pixel_logit, pixel_depth_label, reduction='mean'))
        loss['acc_seg_indice'] = accuracy(
            indice_logit, masked_indice_seg_label, ignore_index=self.indice_ignore_index)
        return loss