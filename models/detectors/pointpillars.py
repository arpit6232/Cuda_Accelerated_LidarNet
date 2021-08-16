"""
Code based on Alex Lang and Oscar Beijbom, 2018.
Licensed under MIT License [see LICENSE].
"""


import time
from enum import Enum
from functools import reduce

import numpy as np
import sparseconvnet as scn
import torch
from torch import nn
from torch.nn import functional as F
import libs 
from libs.tools import metrics


from models.bones.pillars import (PillarFeatureNet, PointPillarsScatter,
                                  PillarFeatureNet_trt, PointPillarsScatter_trt)
from models.bones.rpn import RPN,RPN_trt

from core.losses import (WeightedSigmoidClassificationLoss,
                        WeightedSmoothL1LocalizationLoss,
                        WeightedSoftmaxClassificationLoss)
from libs.ops import box_np_ops,box_torch_ops

class PointPillars(nn.Module):
    def __init__(self,
                output_shape,
                model_cfg,
                target_assigner):
        super().__init__()

        self.name =model_cfg.NAME
        self._pc_range = model_cfg.POINT_CLOUD_RANGE
        self._voxel_size = model_cfg.GRID_SIZE
        self._num_class = model_cfg.NUM_CLASS
        self._use_bev = model_cfg.BACKBONE.use_bev
        self._total_forward_time = 0.0
        self._total_postprocess_time = 0.0
        self._total_inference_count = 0
        
        #for prepare loss weights
        self._pos_cls_weight = model_cfg.pos_cls_weight 
        self._neg_cls_weight = model_cfg.neg_cls_weight 
        self._loss_norm_type = model_cfg.loss_norm_type
        #for create loss 
        self._loc_loss_ftor = model_cfg.loc_loss_ftor
        self._cls_loss_ftor = model_cfg.cls_loss_ftor
        self._dir_loss_ftor = WeightedSoftmaxClassificationLoss()
        self._direction_loss_weight = model_cfg.LOSS.direction_loss_weight
        self._encode_rad_error_by_sin = model_cfg.ENCODE_RAD_ERROR_BY_SIN
        #
        self._cls_loss_weight = model_cfg.cls_weight
        self._loc_loss_weight = model_cfg.loc_weight
        # for direction classifier 
        self._use_direction_classifier = model_cfg.BACKBONE.use_direction_classifier
        # for predict
        self._use_sigmoid_score = model_cfg.POST_PROCESSING.use_sigmoid_score
        self._box_coder = target_assigner.box_coder 
        self.target_assigner = target_assigner
        #for nms 
        self._multiclass_nms = model_cfg.PREDICT.multiclass_nms
        self._use_rotate_nms = model_cfg.PREDICT.use_rotate_nms

        self._nms_score_threshold = model_cfg.POST_PROCESSING.nms_score_threshold
        self._nms_pre_max_size = model_cfg.POST_PROCESSING.nms_pre_max_size
        self._nms_post_max_size = model_cfg.POST_PROCESSING.nms_post_max_size
        self._nms_iou_threshold = model_cfg.POST_PROCESSING.nms_iou_threshold
        self._use_sigmoid_score = model_cfg.POST_PROCESSING.use_sigmoid_score
        # self._use_sigmoid_score = use_sigmoid_score
        self._encode_background_as_zeros=model_cfg.BACKBONE.encode_background_as_zeros
        #1.PFN
        self.pfn = PillarFeatureNet(model_cfg.num_input_features,
                                    model_cfg.PILLAR_FEATURE_EXTRACTOR.use_norm,
                                    num_filters=model_cfg.pfn_num_filters,
                                    with_distance=model_cfg.PILLAR_FEATURE_EXTRACTOR.with_distance,
                                    voxel_size = self._voxel_size,
                                    pc_range = self._pc_range)

        #2.sparse middle
        self.mfe = PointPillarsScatter(output_shape=output_shape,
                                       num_input_features=model_cfg.pfn_num_filters[-1])
        num_rpn_input_filters=self.mfe.nchannels
        #3.rpn
        self.rpn =  RPN(
                        use_norm=model_cfg.BACKBONE.use_norm,
                        num_class=self._num_class,
                        layer_nums=model_cfg.BACKBONE.layer_nums,
                        layer_strides=model_cfg.BACKBONE.layer_strides,
                        num_filters=model_cfg.BACKBONE.num_filters,
                        upsample_strides=model_cfg.BACKBONE.upsample_strides,
                        num_upsample_filters=model_cfg.BACKBONE.num_upsample_filters,
                        num_input_filters=num_rpn_input_filters,
                        num_anchor_per_loc=target_assigner.num_anchors_per_location,
                        encode_background_as_zeros=self._encode_background_as_zeros,
                        use_direction_classifier=model_cfg.BACKBONE.use_direction_classifier,
                        use_bev=model_cfg.BACKBONE.use_bev,
                        use_groupnorm=model_cfg.BACKBONE.use_groupnorm,
                        num_groups=model_cfg.BACKBONE.num_groups,
                        box_code_size=target_assigner.box_coder.code_size
                        )
        self.rpn_acc = metrics.Accuracy(
            dim=-1, encode_background_as_zeros=self._encode_background_as_zeros)
        self.rpn_precision = metrics.Precision(dim=-1)
        self.rpn_recall = metrics.Recall(dim=-1)
        self.rpn_metrics = metrics.PrecisionRecall(
            dim=-1,
            thresholds=[0.1, 0.3, 0.5, 0.7, 0.8, 0.9, 0.95],
            use_sigmoid_score=self._use_sigmoid_score,
            encode_background_as_zeros=self._encode_background_as_zeros)
        
        self.rpn_cls_loss =metrics.Scalar()
        self.rpn_loc_loss =metrics.Scalar()
        self.rpn_total_loss = metrics.Scalar()
        self.register_buffer("global_step", torch.LongTensor(1).zero_())

    def update_global_step(self):
        self.global_step += 1

    def get_global_step(self):
        return int(self.global_step.cpu().numpy()[0])

        
    def forward(self,example):
        '''nutonomy/second.pytorch'''
        voxels = example['voxels']
        num_points = example['num_points']
        coors = example["coordinates"]
        batch_anchors = example["anchors"]        
        batch_size_dev = batch_anchors.shape[0]
        t = time.time()
        # features: [num_voxels, max_num_points_per_voxel, 7]
        # num_points: [num_voxels]
        # coors: [num_voxels, 4]
        voxel_features = self.pfn(voxels, num_points, coors)

        spatial_features = self.mfe(voxel_features,coors,batch_size_dev)
        if self._use_bev:
            preds_dict = self.rpn(spatial_features,example['bev_map'])
        else:
            preds_dict = self.rpn(spatial_features)

        box_preds = preds_dict["box_preds"]
        cls_preds = preds_dict["cls_preds"]
        self._total_forward_time += time.time() - t
        if self.training:
            labels = example['labels']
            reg_targets = example['reg_targets']

            cls_weights, reg_weights, cared = prepare_loss_weights(
                labels,
                pos_cls_weight=self._pos_cls_weight,
                neg_cls_weight=self._neg_cls_weight,
                loss_norm_type=self._loss_norm_type,
                dtype=voxels.dtype)
            cls_targets = labels * cared.type_as(labels)
            cls_targets = cls_targets.unsqueeze(-1)

            loc_loss,cls_loss = create_loss(
                self._loc_loss_ftor,
                self._cls_loss_ftor,
                box_preds=box_preds,
                cls_preds=cls_preds,
                cls_targets=cls_targets,
                cls_weights=cls_weights,
                reg_targets=reg_targets,
                reg_weights=reg_weights,
                num_class=self._num_class,
                encode_rad_error_by_sin=self._encode_rad_error_by_sin,
                encode_background_as_zeros=self._encode_background_as_zeros,
                box_code_size=self._box_coder.code_size,)

            loc_loss_reduced = loc_loss.sum() / batch_size_dev
            loc_loss_reduced *= self._loc_loss_weight
            cls_pos_loss, cls_neg_loss = _get_pos_neg_loss(cls_loss, labels)
            cls_pos_loss /= self._pos_cls_weight
            cls_neg_loss /= self._neg_cls_weight
            cls_loss_reduced = cls_loss.sum() / batch_size_dev
            cls_loss_reduced *= self._cls_loss_weight
            loss = loc_loss_reduced + cls_loss_reduced
            if self._use_direction_classifier:
                dir_targets =  get_direction_target(example['anchors'],
                                                   reg_targets)
                dir_logits = preds_dict["dir_cls_preds"].view(
                    batch_size_dev, -1, 2)
                weights = (labels > 0).type_as(dir_logits)
                weights /= torch.clamp(weights.sum(-1, keepdim=True), min=1.0)
                dir_loss = self._dir_loss_ftor(
                    dir_logits, dir_targets, weights=weights)
                dir_loss = dir_loss.sum() / batch_size_dev
                loss += dir_loss * self._direction_loss_weight

            return {
                "loss": loss,
                "cls_loss": cls_loss,
                "loc_loss": loc_loss,
                "cls_pos_loss": cls_pos_loss,
                "cls_neg_loss": cls_neg_loss,
                "cls_preds": cls_preds,
                "dir_loss_reduced": dir_loss,
                "cls_loss_reduced": cls_loss_reduced,
                "loc_loss_reduced": loc_loss_reduced,
                "cared": cared,
            }
        else:
            return self.predict(example, preds_dict)
    
    def predict(self, example, preds_dict):
        t = time.time()
        batch_size = example['anchors'].shape[0]
        batch_anchors = example["anchors"].view(batch_size, -1, 7)        

        self._total_inference_count += batch_size
        if "anchors_mask" not in example:
            batch_anchors_mask = [None] * batch_size
        else:
            batch_anchors_mask = example["anchors_mask"].view(batch_size, -1)
        batch_imgidx = example['image_idx']

        self._total_forward_time += time.time() - t
        t = time.time()

        batch_box_preds = preds_dict["box_preds"]
        batch_cls_preds = preds_dict["cls_preds"]
        batch_box_preds = batch_box_preds.view(batch_size, -1,
                                               self._box_coder.code_size)
                                

        num_class_with_bg = self._num_class
        if not self._encode_background_as_zeros:
            num_class_with_bg = self._num_class + 1

        batch_cls_preds = batch_cls_preds.view(batch_size, -1,
                                               num_class_with_bg)
        batch_box_preds = self._box_coder.decode_torch(batch_box_preds,
                                                       batch_anchors)
        if self._use_direction_classifier:
            batch_dir_preds = preds_dict["dir_cls_preds"]
            batch_dir_preds = batch_dir_preds.view(batch_size, -1, 2)
        else:
            batch_dir_preds = [None] * batch_size

        predictions_dicts = []

        for box_preds, cls_preds, dir_preds, img_idx, a_mask in zip(
            batch_box_preds, batch_cls_preds, batch_dir_preds,batch_imgidx, batch_anchors_mask):

            if a_mask is not None:
                box_preds = box_preds[a_mask]
                cls_preds = cls_preds[a_mask]
            if self._use_direction_classifier:
                if a_mask is not None:
                    dir_preds = dir_preds[a_mask]
                dir_labels = torch.max(dir_preds,dim = -1)[1]
            if self._encode_background_as_zeros:
                # this don't support softmax
                assert self._use_sigmoid_score is True
                total_scores = torch.sigmoid(cls_preds)
            else:
                # encode background as first element in one-hot vector
                if self._use_sigmoid_score:
                    total_scores = torch.sigmoid(cls_preds)[..., 1:]
                else:
                    total_scores = F.softmax(cls_preds, dim=-1)[..., 1:]
            
            #apply nms in birdeye view
            if self._use_rotate_nms:
                nms_func = box_torch_ops.rotate_nms
            else:
                nms_func = box_torch_ops.nms
            selected_boxes = None
            selected_labels = None
            selected_scores = None
            selected_dir_labels = None

            if self._multiclass_nms:
                #curently only support class-agnostic boxes.
                boxes_for_nms = box_preds[:, [0, 1, 3, 4, 6]]
                if not self._use_rotate_nms:
                    box_preds_corners = box_torch_ops.center_to_corner_box2d(
                        boxes_for_nms[:,:2],boxes_for_nms[:,2:4],
                        boxes_for_nms[:,4]
                    )
                    box_for_nms = box_torch_ops.corner_to_standup_nd(box_preds_corners)
                boxes_for_mcnms = boxes_for_nms.unsqueeze(1)
                selected_per_class = box_torch_ops.multiclass_nms(
                    nms_func=nms_func,
                    boxes=boxes_for_mcnms,
                    scores=total_scores,
                    num_class=self._num_class,
                    pre_max_size=self._nms_pre_max_size,
                    post_max_size=self._nms_post_max_size,
                    iou_threshold=self._nms_iou_threshold,
                    score_thresh=self._nms_score_threshold,
                )
                selected_boxes, selected_labels, selected_scores = [] , [], []
                selected_dir_labels = []
                for i , selected in enumerate(selected_per_class):
                    if selected is not None:
                        num_dets = selected.shape[0]
                        selected_boxes.append(box_preds[selected])
                        selected_labels.append(
                            torch.full([num_dets],i,dtype=torch.int64)
                        )
                        if len(selected_boxes) > 0:
                            selected_boxes = torch.cat(selected_boxes, dim= 0)
                            selected_labels = torch.cat(selected_labels,dim=0)
                            selected_scores = torch.cat(selected_scores,dim = 0)
                            if self._use_direction_classifier:
                                selected_dir_labels = torch.cat(
                                    selected_dir_labels,dim= 0
                                )
                else:
                    selected_boxes = None
                    selected_labels = None
                    selected_scores = None
                    selected_dir_labels = None
            else:
                # get highest score per prediction, than apply nms
                # to remove overlapped box
                if num_class_with_bg ==1:
                    top_scores = total_scores.squeeze(-1)
                    top_labels = torch.zeros(total_scores.shape[0],device=total_scores.device,dtype=torch.long)
                else:
                    top_scores, top_labels = torch.max(total_scores,dim=-1)

                if self._nms_score_threshold > 0.0:
                    thresh = torch.tensor(
                        [self._nms_score_threshold],
                        device = total_scores.device
                    ).type_as(total_scores)
                    top_scores_keep = (top_scores >= thresh)
                    top_scores = top_scores.masked_select(top_scores_keep)
                if top_scores.shape[0] != 0:
                    if self._nms_score_threshold > 0.0:
                        box_preds = box_preds[top_scores_keep]
                        if self._use_direction_classifier:
                            dir_labels = dir_labels[top_scores_keep]
                        top_labels = top_labels[top_scores_keep]
                    boxes_for_nms = box_preds[:,[0,1,3,4,6]]
                    if not self._use_rotate_nms:
                        box_preds_corners = box_torch_ops.center_to_corner_box2d(
                            boxes_for_nms[:, :2], boxes_for_nms[:, 2:4],
                            boxes_for_nms[:, 4])
                        boxes_for_nms = box_torch_ops.corner_to_standup_nd(
                            box_preds_corners)
                    # the nms in 3d detection just remove overlap boxes.
                    selected = nms_func(
                        boxes_for_nms,
                        top_scores,
                        pre_max_size=self._nms_pre_max_size,
                        post_max_size=self._nms_post_max_size,
                        iou_threshold=self._nms_iou_threshold,
                    )
                else:
                    selected = None
                if selected is not None:
                    selected_boxes = box_preds[selected]
                    if self._use_direction_classifier:
                        selected_dir_labels = dir_labels[selected]
                    selected_labels = top_labels[selected]
                    selected_scores = top_scores[selected]

            #finally generate predictions.
            if selected_boxes is not None:
                box_preds = selected_boxes
                scores = selected_scores
                label_preds = selected_labels
                if self._use_direction_classifier:
                    dir_labels = selected_dir_labels
                    opp_labels = (box_preds[..., -1] > 0) ^ dir_labels.bool()
                    box_preds[...,-1] += torch.where(
                        opp_labels,
                        torch.tensor(np.pi).type_as(box_preds),
                        torch.tensor(0.0).type_as(box_preds)
                    )
                final_box_preds = box_preds 
                final_scores = scores 
                final_labels = label_preds
                # predictions
                predictions_dict = {
                    "bbox": None,
                    "box3d_camera": None ,
                    "box3d_lidar": final_box_preds,
                    "scores": final_scores,
                    "label_preds": final_labels,
                    "image_idx": img_idx,
                }
            else:
                predictions_dict = {
                    "bbox": None,
                    "box3d_camera": None,
                    "box3d_lidar": None,
                    "scores": None,
                    "label_preds": None,
                    "image_idx": img_idx,
                }
            predictions_dicts.append(predictions_dict)
        self._total_postprocess_time += time.time() - t
        return predictions_dicts

    @property
    def avg_forward_time(self):
        return self._total_forward_time / self._total_inference_count

    @property
    def avg_postprocess_time(self):
        return self._total_postprocess_time / self._total_inference_count

    def clear_time_metrics(self):
        self._total_forward_time = 0.0
        self._total_postprocess_time = 0.0
        self._total_inference_count = 0

    def metrics_to_float(self):
        self.rpn_acc.float()
        self.rpn_metrics.float()
        self.rpn_cls_loss.float()
        self.rpn_loc_loss.float()
        self.rpn_total_loss.float()
    
    def update_metrics(self,
                       cls_loss,
                       loc_loss,
                       cls_preds,
                       labels,
                       sampled):
        batch_size = cls_preds.shape[0]
        num_class = self._num_class
        if not self._encode_background_as_zeros:
            num_class += 1
        cls_preds = cls_preds.view(batch_size, -1, num_class)
        rpn_acc = self.rpn_acc(labels, cls_preds, sampled).numpy()[0]
        prec, recall = self.rpn_metrics(labels, cls_preds, sampled)
        prec = prec.numpy()
        recall = recall.numpy()
        rpn_cls_loss = self.rpn_cls_loss(cls_loss).numpy()[0]
        rpn_loc_loss = self.rpn_loc_loss(loc_loss).numpy()[0]
        ret = {
            "cls_loss": float(rpn_cls_loss),
            "cls_loss_rt": float(cls_loss.data.cpu().numpy()),
            'loc_loss': float(rpn_loc_loss),
            "loc_loss_rt": float(loc_loss.data.cpu().numpy()),
            "rpn_acc": float(rpn_acc),
        }
        for i, thresh in enumerate(self.rpn_metrics.thresholds):
            ret[f"prec@{int(thresh*100)}"] = float(prec[i])
            ret[f"rec@{int(thresh*100)}"] = float(recall[i])
        return ret
    
    def clear_metrics(self):
        self.rpn_acc.clear()
        self.rpn_metrics.clear()
        self.rpn_cls_loss.clear()
        self.rpn_loc_loss.clear()
        self.rpn_total_loss.clear()

    @staticmethod
    def convert_norm_to_float(net):
        '''
        BatchNorm layers to have parameters in single precision.
        Find all layers and convert them back to float. This can't
        be done with built in .apply as that function will apply
        fn to all modules, parameters, and buffers. Thus we wouldn't
        be able to guard the float conversion based on the module type.
        '''
        if isinstance(net, torch.nn.modules.batchnorm._BatchNorm):
            net.float()
        for child in net.children():
            PointPillars.convert_norm_to_float(net)
        return net
    

class PointPillars_trt(nn.Module):
    def __init__(self,
                output_shape,
                model_cfg,
                target_assigner):
        super().__init__()

        self.name =model_cfg.NAME
        self._pc_range = model_cfg.POINT_CLOUD_RANGE
        self._voxel_size = model_cfg.GRID_SIZE
        self._num_class = model_cfg.NUM_CLASS
        self._use_bev = model_cfg.BACKBONE.use_bev
        self._total_forward_time = 0.0
        self._total_postprocess_time = 0.0
        self._total_inference_count = 0
        
        # #for prepare loss weights
        # self._pos_cls_weight = model_cfg.pos_cls_weight 
        # self._neg_cls_weight = model_cfg.neg_cls_weight 
        # self._loss_norm_type = model_cfg.loss_norm_type
        # #for create loss 
        # self._loc_loss_ftor = model_cfg.loc_loss_ftor
        # self._cls_loss_ftor = model_cfg.cls_loss_ftor
        # self._dir_loss_ftor = WeightedSoftmaxClassificationLoss()
        # self._direction_loss_weight = model_cfg.LOSS.direction_loss_weight
        # self._encode_rad_error_by_sin = model_cfg.ENCODE_RAD_ERROR_BY_SIN
        # #
        # self._cls_loss_weight = model_cfg.cls_weight
        # self._loc_loss_weight = model_cfg.loc_weight
        # for direction classifier 
        self._use_direction_classifier = model_cfg.BACKBONE.use_direction_classifier
        # for predict
        self._use_sigmoid_score = model_cfg.POST_PROCESSING.use_sigmoid_score
        self._box_coder = target_assigner.box_coder 
        self.target_assigner = target_assigner
        #for nms 
        self._multiclass_nms = model_cfg.PREDICT.multiclass_nms
        self._use_rotate_nms = model_cfg.PREDICT.use_rotate_nms

        self._nms_score_threshold = model_cfg.POST_PROCESSING.nms_score_threshold
        self._nms_pre_max_size = model_cfg.POST_PROCESSING.nms_pre_max_size
        self._nms_post_max_size = model_cfg.POST_PROCESSING.nms_post_max_size
        self._nms_iou_threshold = model_cfg.POST_PROCESSING.nms_iou_threshold
        self._use_sigmoid_score = model_cfg.POST_PROCESSING.use_sigmoid_score
        # self._use_sigmoid_score = use_sigmoid_score
        self._encode_background_as_zeros=model_cfg.BACKBONE.encode_background_as_zeros
        #1.PFN
        self.pfn = PillarFeatureNet_trt(model_cfg.num_input_features,
                                    model_cfg.PILLAR_FEATURE_EXTRACTOR.use_norm,
                                    num_filters=model_cfg.pfn_num_filters,
                                    with_distance=model_cfg.PILLAR_FEATURE_EXTRACTOR.with_distance,
                                    voxel_size = self._voxel_size,
                                    pc_range = self._pc_range)

        #2.sparse middle
        self.mfe = PointPillarsScatter_trt(output_shape=output_shape,
                                       num_input_features=model_cfg.pfn_num_filters[-1])
        num_rpn_input_filters=self.mfe.nchannels
        #3.rpn
        self.rpn =  RPN_trt(
                        use_norm=model_cfg.BACKBONE.use_norm,
                        num_class=self._num_class,
                        layer_nums=model_cfg.BACKBONE.layer_nums,
                        layer_strides=model_cfg.BACKBONE.layer_strides,
                        num_filters=model_cfg.BACKBONE.num_filters,
                        upsample_strides=model_cfg.BACKBONE.upsample_strides,
                        num_upsample_filters=model_cfg.BACKBONE.num_upsample_filters,
                        num_input_filters=num_rpn_input_filters,
                        num_anchor_per_loc=target_assigner.num_anchors_per_location,
                        encode_background_as_zeros=self._encode_background_as_zeros,
                        use_direction_classifier=model_cfg.BACKBONE.use_direction_classifier,
                        use_bev=model_cfg.BACKBONE.use_bev,
                        use_groupnorm=model_cfg.BACKBONE.use_groupnorm,
                        num_groups=model_cfg.BACKBONE.num_groups,
                        box_code_size=target_assigner.box_coder.code_size
                        )
 

        
    def forward(self,example):
        '''nutonomy/second.pytorch'''
        # training input [0:pillar_x, 1:pillar_y, 2:pillar_z, 3:pillar_i,
        #                 4:num_points_per_pillar, 5:x_sub_shaped, 6:y_sub_shaped, 7:mask, 8:coors
        #                 9:voxel_mask, 10:anchors, #11:anchors_mask]
        #print(len(example))
        pillar_x = example[0]
        pillar_y = example[1]
        pillar_z = example[2]
        pillar_i = example[3]
        num_points = example[4]
        x_sub_shaped = example[5]
        y_sub_shaped = example[6]
        mask = example[7]
        #print(pillar_x.shape)

        # features: [num_voxels, max_num_points_per_voxel, 7]
        # num_points: [num_voxels]
        # coors: [num_voxels, 4]
        voxel_features = self.pfn(pillar_x, pillar_y, pillar_z, pillar_i,
                                  num_points, x_sub_shaped, y_sub_shaped, mask)
        voxel_features = voxel_features.squeeze()
        voxel_features = voxel_features.permute(1, 0)
        #print(voxel_features.shape)
        coors = example[8]
        spatial_features = self.mfe(voxel_features, coors,example[9])
        # spatial_features input size is : [1, 64, 496, 432]

        preds_dict = self.rpn(spatial_features)

        return self.predict(example, preds_dict)
    
    def predict(self, example, preds_dict):
        batch_size = 1
        batch_anchors = example[10].view(batch_size, -1, 7)

        self._total_inference_count += batch_size
        # batch_rect = example[11]
        # batch_Trv2c = example[12]
        # batch_P2 = example[13]
        
        batch_anchors_mask = [None] * batch_size
        
        # if "anchors_mask" not in example:
        #     batch_anchors_mask = [None] * batch_size
        # else:
        #     batch_anchors_mask = example["anchors_mask"].view(batch_size, -1)
        # assert 15==len(example), "somthing write with example size!"
        #batch_anchors_mask = anchors_mask.view(batch_size, -1)
        # batch_imgidx = example['image_idx']
        batch_imgidx = example[11]

        # self._total_forward_time += time.time() - t
        # t = time.time()
        batch_box_preds = preds_dict[0]
        batch_cls_preds = preds_dict[1]
        batch_box_preds = batch_box_preds.view(batch_size, -1,
                                               self._box_coder.code_size)
        num_class_with_bg = self._num_class
        if not self._encode_background_as_zeros:
            num_class_with_bg = self._num_class + 1

        batch_cls_preds = batch_cls_preds.view(batch_size, -1,
                                               num_class_with_bg)
        batch_box_preds = self._box_coder.decode_torch(batch_box_preds,
                                                       batch_anchors)
        if self._use_direction_classifier:
            batch_dir_preds = preds_dict[2]
            batch_dir_preds = batch_dir_preds.view(batch_size, -1, 2)
        else:
            batch_dir_preds = [None] * batch_size

        # predictions_dicts = []
        predictions_dicts = ()
        for box_preds, cls_preds, dir_preds,  img_idx, a_mask in zip(
                batch_box_preds, batch_cls_preds, batch_dir_preds, batch_imgidx, batch_anchors_mask):
            if a_mask is not None:
                box_preds = box_preds[a_mask]
                cls_preds = cls_preds[a_mask]
            if self._use_direction_classifier:
                if a_mask is not None:
                    dir_preds = dir_preds[a_mask]
                # print(dir_preds.shape)
                dir_labels = torch.max(dir_preds, dim=-1)[1]
            if self._encode_background_as_zeros:
                # this don't support softmax
                assert self._use_sigmoid_score is True
                total_scores = torch.sigmoid(cls_preds)
            else:
                # encode background as first element in one-hot vector
                if self._use_sigmoid_score:
                    total_scores = torch.sigmoid(cls_preds)[..., 1:]
                else:
                    total_scores = F.softmax(cls_preds, dim=-1)[..., 1:]
            # Apply NMS in birdeye view
            if self._use_rotate_nms:
                nms_func = box_torch_ops.rotate_nms
            else:
                nms_func = box_torch_ops.nms
            selected_boxes = None
            selected_labels = None
            selected_scores = None
            selected_dir_labels = None

            if self._multiclass_nms:
                # curently only support class-agnostic boxes.
                boxes_for_nms = box_preds[:, [0, 1, 3, 4, 6]]
                if not self._use_rotate_nms:
                    box_preds_corners = box_torch_ops.center_to_corner_box2d(
                        boxes_for_nms[:, :2], boxes_for_nms[:, 2:4],
                        boxes_for_nms[:, 4])
                    boxes_for_nms = box_torch_ops.corner_to_standup_nd(
                        box_preds_corners)
                boxes_for_mcnms = boxes_for_nms.unsqueeze(1)
                selected_per_class = box_torch_ops.multiclass_nms(
                    nms_func=nms_func,
                    boxes=boxes_for_mcnms,
                    scores=total_scores,
                    num_class=self._num_class,
                    pre_max_size=self._nms_pre_max_size,
                    post_max_size=self._nms_post_max_size,
                    iou_threshold=self._nms_iou_threshold,
                    score_thresh=self._nms_score_threshold,
                )
                selected_boxes, selected_labels, selected_scores = [], [], []
                selected_dir_labels = []
                for i, selected in enumerate(selected_per_class):
                    if selected is not None:
                        num_dets = selected.shape[0]
                        selected_boxes.append(box_preds[selected])
                        selected_labels.append(
                            torch.full([num_dets], i, dtype=torch.int64))
                        if self._use_direction_classifier:
                            selected_dir_labels.append(dir_labels[selected])
                        selected_scores.append(total_scores[selected, i])
                if len(selected_boxes) > 0:
                    selected_boxes = torch.cat(selected_boxes, dim=0)
                    selected_labels = torch.cat(selected_labels, dim=0)
                    selected_scores = torch.cat(selected_scores, dim=0)
                    if self._use_direction_classifier:
                        selected_dir_labels = torch.cat(
                            selected_dir_labels, dim=0)
                else:
                    selected_boxes = None
                    selected_labels = None
                    selected_scores = None
                    selected_dir_labels = None
            else:
                # get highest score per prediction, than apply nms
                # to remove overlapped box.
                if num_class_with_bg == 1:
                    top_scores = total_scores.squeeze(-1)
                    top_labels = torch.zeros(
                        total_scores.shape[0],
                        device=total_scores.device,
                        dtype=torch.long)
                else:
                    top_scores, top_labels = torch.max(total_scores, dim=-1)

                if self._nms_score_threshold > 0.0:
                    thresh = torch.tensor(
                        [self._nms_score_threshold],
                        device=total_scores.device).type_as(total_scores)
                    top_scores_keep = (top_scores >= thresh)
                    top_scores = top_scores.masked_select(top_scores_keep)
                if top_scores.shape[0] != 0:
                    if self._nms_score_threshold > 0.0:
                        box_preds = box_preds[top_scores_keep]
                        if self._use_direction_classifier:
                            dir_labels = dir_labels[top_scores_keep]
                        top_labels = top_labels[top_scores_keep]
                    boxes_for_nms = box_preds[:, [0, 1, 3, 4, 6]]
                    if not self._use_rotate_nms:
                        box_preds_corners = box_torch_ops.center_to_corner_box2d(
                            boxes_for_nms[:, :2], boxes_for_nms[:, 2:4],
                            boxes_for_nms[:, 4])
                        boxes_for_nms = box_torch_ops.corner_to_standup_nd(
                            box_preds_corners)
                    # the nms in 3d detection just remove overlap boxes.
                    selected = nms_func(
                        boxes_for_nms,
                        top_scores,
                        pre_max_size=self._nms_pre_max_size,
                        post_max_size=self._nms_post_max_size,
                        iou_threshold=self._nms_iou_threshold,
                    )
                else:
                    selected = None
                if selected is not None:
                    selected_boxes = box_preds[selected]
                    if self._use_direction_classifier:
                        selected_dir_labels = dir_labels[selected]
                    selected_labels = top_labels[selected]
                    selected_scores = top_scores[selected]
            # finally generate predictions.

            if selected_boxes is not None:
                box_preds = selected_boxes
                scores = selected_scores
                label_preds = selected_labels
                if self._use_direction_classifier:
                    dir_labels = selected_dir_labels
                    opp_labels = (box_preds[..., -1] > 0) ^ (dir_labels.byte() > 0)
                    box_preds[..., -1] += torch.where(
                        opp_labels,
                        torch.tensor(np.pi).type_as(box_preds),
                        torch.tensor(0.0).type_as(box_preds))
                    # box_preds[..., -1] += (
                    #     ~(dir_labels.byte())).type_as(box_preds) * np.pi
                final_box_preds = box_preds
                final_scores = scores
                final_labels = label_preds

                predictions_dict = (final_box_preds, final_scores, label_preds, img_idx)
            else:
                predictions_dict = (None, None, None, None, None, img_idx)
            # predictions_dicts.append(predictions_dict)
            predictions_dicts += (predictions_dict, )
        #self._total_postprocess_time += time.time() - t
        return predictions_dicts


def prepare_loss_weights(labels,
                         pos_cls_weight= 1.0,
                         neg_cls_weight= 1.0,
                         loss_norm_type= 'NormByNumPositives',
                         dtype= torch.float32):

    cared = labels >= 0
    #cared : [N,num_anchors]
    positives = labels >0 
    negatives = labels ==0
    negatives_cls_weights = negatives.type(dtype) * neg_cls_weight
    cls_weights = neg_cls_weight + pos_cls_weight * positives.type(dtype)
    reg_weights = positives.type(dtype)
    if loss_norm_type == 'NormByNumExamples':
        num_examples = cared.type(dtype).sum(1, keepdim=True)
        num_examples = torch.clamp(num_examples, min=1.0)
        cls_weights /= num_examples
        bbox_normalizer = positives.sum(1, keepdim=True).type(dtype)
        reg_weights /= torch.clamp(bbox_normalizer, min=1.0)
    elif loss_norm_type == 'NormByNumPositives':  # for focal loss
        pos_normalizer = positives.sum(1, keepdim=True).type(dtype)
        reg_weights /= torch.clamp(pos_normalizer, min=1.0)
        cls_weights /= torch.clamp(pos_normalizer, min=1.0)
    elif loss_norm_type == 'NormByNumPosNeg':
        pos_neg = torch.stack([positives, negatives], dim=-1).type(dtype)
        normalizer = pos_neg.sum(1, keepdim=True)  # [N, 1, 2]
        cls_normalizer = (pos_neg * normalizer).sum(-1)  # [N, M]
        cls_normalizer = torch.clamp(cls_normalizer, min=1.0)
        # cls_normalizer will be pos_or_neg_weight/num_pos_or_neg
        normalizer = torch.clamp(normalizer, min=1.0)
        reg_weights /= normalizer[:, 0:1, 0]
        cls_weights /= cls_normalizer
    else:
        raise ValueError(
            f"unknown loss norm type. available: {list(LossNormType)}")
    return cls_weights, reg_weights, cared

def create_loss(loc_loss_ftor,
                cls_loss_ftor,
                box_preds,
                cls_preds,
                cls_targets,
                cls_weights,
                reg_targets,
                reg_weights,
                num_class,
                encode_background_as_zeros=True,
                encode_rad_error_by_sin=True,
                box_code_size = 7):
    batch_size = int(box_preds.shape[0])
    box_preds = box_preds.view(batch_size, -1, box_code_size)
    if encode_background_as_zeros:
        cls_preds = cls_preds.view(batch_size,-1,num_class)
    else:
        cls_preds = cls_preds.view(batch_size,-1,num_class+1)
    cls_targets = cls_targets.squeeze(-1)
    one_hot_targets = libs.tools.one_hot(
        cls_targets, depth=num_class+1,dtype=box_preds.dtype
    )
    if encode_background_as_zeros:
        one_hot_targets = one_hot_targets[..., 1:]
    if encode_rad_error_by_sin:
        # sin(a-b) = sina*cosb-cosa*sinb
        box_preds, reg_targets = add_sin_difference(box_preds, reg_targets)
    loc_losses = loc_loss_ftor(
        box_preds, reg_targets, weights=reg_weights)  # [N, M]
    cls_losses = cls_loss_ftor(
        cls_preds, one_hot_targets, weights=cls_weights)  # [N, M]
    return loc_losses, cls_losses

def add_sin_difference(boxes1, boxes2):
    rad_pred_encoding = torch.sin(boxes1[..., -1:]) * torch.cos(
        boxes2[..., -1:])
    rad_tg_encoding = torch.cos(boxes1[..., -1:]) * torch.sin(boxes2[..., -1:])
    boxes1 = torch.cat([boxes1[..., :-1], rad_pred_encoding], dim=-1)
    boxes2 = torch.cat([boxes2[..., :-1], rad_tg_encoding], dim=-1)
    return boxes1, boxes2

def _get_pos_neg_loss(cls_loss, labels):
    # cls_loss: [N, num_anchors, num_class]
    # labels: [N, num_anchors]
    batch_size = cls_loss.shape[0]
    if cls_loss.shape[-1] == 1 or len(cls_loss.shape) == 2:
        cls_pos_loss = (labels > 0).type_as(cls_loss) * cls_loss.view(
            batch_size, -1)
        cls_neg_loss = (labels == 0).type_as(cls_loss) * cls_loss.view(
            batch_size, -1)
        cls_pos_loss = cls_pos_loss.sum() / batch_size
        cls_neg_loss = cls_neg_loss.sum() / batch_size
    else:
        cls_pos_loss = cls_loss[..., 1:].sum() / batch_size
        cls_neg_loss = cls_loss[..., 0].sum() / batch_size
    return cls_pos_loss, cls_neg_loss

def get_direction_target(anchors, reg_targets, one_hot=True):
    batch_size = reg_targets.shape[0]
    anchors = anchors.view(batch_size, -1, 7)
    rot_gt = reg_targets[..., -1] + anchors[..., -1]
    dir_cls_targets = (rot_gt > 0).long()
    if one_hot:
        dir_cls_targets = libs.tools.one_hot(
            dir_cls_targets, 2, dtype=anchors.dtype)
    return dir_cls_targets
