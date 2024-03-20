import copy
from functools import partial
import numpy as np
from mmdet.datasets import DATASETS
from mmdet3d.datasets import NuScenesDataset
import mmcv
import os
from os import path as osp
from mmdet.datasets import DATASETS
import torch
import numpy as np
from nuscenes.eval.common.utils import quaternion_yaw, Quaternion
from .nuscnes_eval import NuScenesEval_custom
from projects.mmdet3d_plugin.models.utils.visual import save_tensor
from mmcv.parallel import DataContainer as DC
import random
import prettytable
from .nuscenes_dataset import CustomNuScenesDataset
from nuscenes.map_expansion.map_api import NuScenesMap, NuScenesMapExplorer
from nuscenes.eval.common.utils import quaternion_yaw, Quaternion
from shapely import affinity, ops
from shapely.geometry import LineString, box, MultiPolygon, MultiLineString
from mmdet.datasets.pipelines import to_tensor
import json
import cv2
import math # TODO multi gpu

def add_rotation_noise(extrinsics, std=0.01, mean=0.0):
    #n = extrinsics.shape[0]
    noise_angle = torch.normal(mean, std=std, size=(3,))
    # extrinsics[:, 0:3, 0:3] *= (1 + noise)
    sin_noise = torch.sin(noise_angle)
    cos_noise = torch.cos(noise_angle)
    rotation_matrix = torch.eye(4).view(4, 4)
    #  rotation_matrix[]
    rotation_matrix_x = rotation_matrix.clone()
    rotation_matrix_x[1, 1] = cos_noise[0]
    rotation_matrix_x[1, 2] = sin_noise[0]
    rotation_matrix_x[2, 1] = -sin_noise[0]
    rotation_matrix_x[2, 2] = cos_noise[0]

    rotation_matrix_y = rotation_matrix.clone()
    rotation_matrix_y[0, 0] = cos_noise[1]
    rotation_matrix_y[0, 2] = -sin_noise[1]
    rotation_matrix_y[2, 0] = sin_noise[1]
    rotation_matrix_y[2, 2] = cos_noise[1]

    rotation_matrix_z = rotation_matrix.clone()
    rotation_matrix_z[0, 0] = cos_noise[2]
    rotation_matrix_z[0, 1] = sin_noise[2]
    rotation_matrix_z[1, 0] = -sin_noise[2]
    rotation_matrix_z[1, 1] = cos_noise[2]

    rotation_matrix = rotation_matrix_x @ rotation_matrix_y @ rotation_matrix_z

    rotation = torch.from_numpy(extrinsics.astype(np.float32))
    rotation[:3, -1] = 0.0
    # import pdb;pdb.set_trace()
    rotation = rotation_matrix @ rotation
    extrinsics[:3, :3] = rotation[:3, :3].numpy()
    return extrinsics


def add_translation_noise(extrinsics, std=0.01, mean=0.0):
    # n = extrinsics.shape[0]
    noise = torch.normal(mean, std=std, size=(3,))
    extrinsics[0:3, -1] += noise.numpy()
    return extrinsics

def perspective(cam_coords, proj_mat):
    pix_coords = proj_mat @ cam_coords
    valid_idx = pix_coords[2, :] > 0
    pix_coords = pix_coords[:, valid_idx]
    pix_coords = pix_coords[:2, :] / (pix_coords[2, :] + 1e-7)
    pix_coords = pix_coords.transpose(1, 0)
    return pix_coords
class LiDARInstanceLines(object):
    """Line instance in LIDAR coordinates

    """
    def __init__(self, 
                 instance_line_list,
                 instance_labels,
                 sample_dist=1,
                 num_samples=250,
                 padding=False,
                 fixed_num=-1,
                 Ext_fixed_num=-1,
                 padding_value=-10000,
                 patch_size=None):
        assert isinstance(instance_line_list, list)
        assert patch_size is not None
        if len(instance_line_list) != 0:
            assert isinstance(instance_line_list[0], LineString)
        self.patch_size = patch_size
        self.max_x = self.patch_size[1] / 2
        self.max_y = self.patch_size[0] / 2
        self.sample_dist = sample_dist
        self.num_samples = num_samples
        self.padding = padding
        self.fixed_num = fixed_num
        self.Ext_fixed_num = Ext_fixed_num
        self.padding_value = padding_value

        self.instance_list = instance_line_list
        self.instance_labels = instance_labels
        self.fixed_dist = 5                 # TODO 추가
        self.padding_length = 36            # TODO 추가

    @property
    def start_end_points(self):
        """
        return torch.Tensor([N,4]), in xstart, ystart, xend, yend form
        """
        assert len(self.instance_list) != 0
        instance_se_points_list = []
        for instance in self.instance_list:
            se_points = []
            se_points.extend(instance.coords[0])
            se_points.extend(instance.coords[-1])
            instance_se_points_list.append(se_points)
        instance_se_points_array = np.array(instance_se_points_list)
        instance_se_points_tensor = to_tensor(instance_se_points_array)
        instance_se_points_tensor = instance_se_points_tensor.to(
                                dtype=torch.float32)
        instance_se_points_tensor[:,0] = torch.clamp(instance_se_points_tensor[:,0], min=-self.max_x,max=self.max_x)
        instance_se_points_tensor[:,1] = torch.clamp(instance_se_points_tensor[:,1], min=-self.max_y,max=self.max_y)
        instance_se_points_tensor[:,2] = torch.clamp(instance_se_points_tensor[:,2], min=-self.max_x,max=self.max_x)
        instance_se_points_tensor[:,3] = torch.clamp(instance_se_points_tensor[:,3], min=-self.max_y,max=self.max_y)
        return instance_se_points_tensor

    @property
    def bbox(self):
        """
        return torch.Tensor([N,4]), in xmin, ymin, xmax, ymax form
        """
        assert len(self.instance_list) != 0
        instance_bbox_list = []
        for instance in self.instance_list:
            # bounds is bbox: [xmin, ymin, xmax, ymax]
            instance_bbox_list.append(instance.bounds)
        instance_bbox_array = np.array(instance_bbox_list)
        instance_bbox_tensor = to_tensor(instance_bbox_array)
        instance_bbox_tensor = instance_bbox_tensor.to(
                            dtype=torch.float32)
        instance_bbox_tensor[:,0] = torch.clamp(instance_bbox_tensor[:,0], min=-self.max_x,max=self.max_x)
        instance_bbox_tensor[:,1] = torch.clamp(instance_bbox_tensor[:,1], min=-self.max_y,max=self.max_y)
        instance_bbox_tensor[:,2] = torch.clamp(instance_bbox_tensor[:,2], min=-self.max_x,max=self.max_x)
        instance_bbox_tensor[:,3] = torch.clamp(instance_bbox_tensor[:,3], min=-self.max_y,max=self.max_y)
        return instance_bbox_tensor

    @property
    def bbox_condi(self):
        """
        return torch.Tensor([N,4]), in xmin, ymin, xmax, ymax form
        """
        assert len(self.instance_list) != 0
        instance_bbox_list = []
        for instance in self.instance_list:
            if instance.length < 2:
                continue
            # bounds is bbox: [xmin, ymin, xmax, ymax]
            instance_bbox_list.append(instance.bounds)
        if len(instance_bbox_list) == 0:
            for instance in self.instance_list:
                instance_bbox_list.append(instance.bounds)
        instance_bbox_array = np.array(instance_bbox_list)
        instance_bbox_tensor = to_tensor(instance_bbox_array)
        instance_bbox_tensor = instance_bbox_tensor.to(
            dtype=torch.float32)
        instance_bbox_tensor[:, 0] = torch.clamp(instance_bbox_tensor[:, 0], min=-self.max_x, max=self.max_x)
        instance_bbox_tensor[:, 1] = torch.clamp(instance_bbox_tensor[:, 1], min=-self.max_y, max=self.max_y)
        instance_bbox_tensor[:, 2] = torch.clamp(instance_bbox_tensor[:, 2], min=-self.max_x, max=self.max_x)
        instance_bbox_tensor[:, 3] = torch.clamp(instance_bbox_tensor[:, 3], min=-self.max_y, max=self.max_y)
        return instance_bbox_tensor

    @property
    def gt_labels(self):
        instances_list = []
        for idx, instance in enumerate(self.instance_list):
            if instance.length < 2:
                continue
            instance_label = self.instance_labels[idx]
            instances_list.append(instance_label)
        if len(instances_list) == 0:
            for idx, instance in enumerate(self.instance_list):
                instance_label = self.instance_labels[idx]
                instances_list.append(instance_label)
        instance_bbox_array = np.array(instances_list)
        instance_bbox_tensor = to_tensor(instance_bbox_array)
        return instance_bbox_tensor

    @property
    def fixed_num_sampled_points(self):
        """
        return torch.Tensor([N,fixed_num,2]), in xmin, ymin, xmax, ymax form
            N means the num of instances
        """
        assert len(self.instance_list) != 0
        instance_points_list = []
        for instance in self.instance_list:
            distances = np.linspace(0, instance.length, self.fixed_num)
            sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            instance_points_list.append(sampled_points)
        instance_points_array = np.array(instance_points_list)
        instance_points_tensor = to_tensor(instance_points_array)
        instance_points_tensor = instance_points_tensor.to(
                            dtype=torch.float32)
        instance_points_tensor[:,:,0] = torch.clamp(instance_points_tensor[:,:,0], min=-self.max_x,max=self.max_x)
        instance_points_tensor[:,:,1] = torch.clamp(instance_points_tensor[:,:,1], min=-self.max_y,max=self.max_y)
        return instance_points_tensor

    @property
    def fixed_num_sampled_points_dou(self):
        """
        return torch.Tensor([N,fixed_num,2]), in xmin, ymin, xmax, ymax form
            N means the num of instances
        """
        assert len(self.instance_list) != 0
        instance_points_list = []
        for instance in self.instance_list:
            distances = np.linspace(0, instance.length, self.fixed_num*2)
            sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(
                -1, 2)
            instance_points_list.append(sampled_points)
        instance_points_array = np.array(instance_points_list)
        instance_points_tensor = to_tensor(instance_points_array)
        instance_points_tensor = instance_points_tensor.to(
            dtype=torch.float32)
        instance_points_tensor[:, :, 0] = torch.clamp(instance_points_tensor[:, :, 0], min=-self.max_x, max=self.max_x)
        instance_points_tensor[:, :, 1] = torch.clamp(instance_points_tensor[:, :, 1], min=-self.max_y, max=self.max_y)
        return instance_points_tensor

    @property
    def Ext_fixed_num_sampled_points(self):
        assert len(self.instance_list) != 0
        instance_points_list = []
        for instance in self.instance_list:
            distances = np.linspace(0, instance.length, self.Ext_fixed_num)
            sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(
                -1, 2)
            instance_points_list.append(sampled_points)
        instance_points_array = np.array(instance_points_list)
        instance_points_tensor = to_tensor(instance_points_array)
        instance_points_tensor = instance_points_tensor.to(
            dtype=torch.float32)
        instance_points_tensor[:, :, 0] = torch.clamp(instance_points_tensor[:, :, 0], min=-self.max_x, max=self.max_x)
        instance_points_tensor[:, :, 1] = torch.clamp(instance_points_tensor[:, :, 1], min=-self.max_y, max=self.max_y)
        return instance_points_tensor

    @property
    def Class_segemnts(self):
        assert len(self.instance_list) != 0
        self.canvas_size = [200, 100]
        self.scale_x = self.canvas_size[1] / self.patch_size[1]
        self.scale_y = self.canvas_size[0] / self.patch_size[0]
        instance_points_list = []
        gt_semantic_mask_0 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
        gt_semantic_mask_1 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
        gt_semantic_mask_2 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)

        for idx, instance in enumerate(self.instance_list):
            instance_label = self.instance_labels[idx]
            if instance_label == 0:
                self.line_ego_to_mask(instance, gt_semantic_mask_0[0], color=1, thickness=3)
            elif instance_label == 1:
                self.line_ego_to_mask(instance, gt_semantic_mask_1[0], color=1, thickness=3)
            elif instance_label == 2:
                self.line_ego_to_mask(instance, gt_semantic_mask_2[0], color=1, thickness=3)

        gt_semantic_mask = np.concatenate([gt_semantic_mask_0.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])
                        , gt_semantic_mask_1.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])
                        , gt_semantic_mask_2.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])], 1)
        
        return to_tensor(gt_semantic_mask).float()

    @property
    def Class_segemnts_v2(self):
        assert len(self.instance_list) != 0
        self.canvas_size = [200, 100]
        self.scale_x = self.canvas_size[1] / self.patch_size[1]
        self.scale_y = self.canvas_size[0] / self.patch_size[0]
        instance_points_list = []
        gt_semantic_mask_0 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
        gt_semantic_mask_1 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
        gt_semantic_mask_2 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)

        for idx, instance in enumerate(self.instance_list):
            instance_label = self.instance_labels[idx]
            if instance_label == 0:
                self.line_ego_to_mask(instance, gt_semantic_mask_0[0], color=1, thickness=1)
            elif instance_label == 1:
                self.line_ego_to_mask(instance, gt_semantic_mask_1[0], color=1, thickness=1)
            elif instance_label == 2:
                self.line_ego_to_mask(instance, gt_semantic_mask_2[0], color=1, thickness=1)

        gt_semantic_mask = np.concatenate([gt_semantic_mask_0.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])
                                              ,
                                           gt_semantic_mask_1.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])
                                              ,
                                           gt_semantic_mask_2.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])],
                                          1)

        return to_tensor(gt_semantic_mask).float()

    @property
    def Class_segemnts_v3(self):
        assert len(self.instance_list) != 0
        self.canvas_size = [200, 100]
        self.scale_x = self.canvas_size[1] / self.patch_size[1]
        self.scale_y = self.canvas_size[0] / self.patch_size[0]
        instance_points_list = []
        gt_semantic_mask_0 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
        gt_semantic_mask_1 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
        gt_semantic_mask_2 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)

        for idx, instance in enumerate(self.instance_list):
            instance_label = self.instance_labels[idx]
            if instance_label == 0:
                self.line_ego_to_mask(instance, gt_semantic_mask_0[0], color=1, thickness=5)
            elif instance_label == 1:
                self.line_ego_to_mask(instance, gt_semantic_mask_1[0], color=1, thickness=5)
            elif instance_label == 2:
                self.line_ego_to_mask(instance, gt_semantic_mask_2[0], color=1, thickness=5)

        gt_semantic_mask = np.concatenate([gt_semantic_mask_0.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])
                                              ,
                                           gt_semantic_mask_1.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])
                                              ,
                                           gt_semantic_mask_2.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])],
                                          1)

        return to_tensor(gt_semantic_mask).float()

    def line_ego_to_mask(self,
                         line_ego,
                         mask,
                         color=1,
                         thickness=3):
        ''' Rasterize a single line to mask.

        Args:
            line_ego (LineString): line
            mask (array): semantic mask to paint on
            color (int): positive label, default: 1
            thickness (int): thickness of rasterized lines, default: 3
        '''
        self.canvas_size = [200, 100]
        trans_x = self.canvas_size[1] / 2
        trans_y = self.canvas_size[0] / 2
        line_ego = affinity.scale(line_ego, self.scale_x, self.scale_y, origin=(0, 0))
        line_ego = affinity.affine_transform(line_ego, [1.0, 0.0, 0.0, 1.0, trans_x, trans_y])
        # print(np.array(list(line_ego.coords), dtype=np.int32).shape)
        coords = np.array(list(line_ego.coords), dtype=np.int32)[:, :2]
        coords = coords.reshape((-1, 2))
        assert len(coords) >= 2
        cv2.polylines(mask, np.int32([coords]), False, color=color, thickness=thickness)

    @property
    def fixed_num_sampled_points_condi(self):
        """
        return torch.Tensor([N,fixed_num,2]), in xmin, ymin, xmax, ymax form
            N means the num of instances
        """
        assert len(self.instance_list) != 0
        instance_points_list = []
        for instance in self.instance_list:
            if instance.length < 2:
                continue
            distances = np.linspace(0, instance.length, self.fixed_num)
            sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(
                -1, 2)
            instance_points_list.append(sampled_points)
        if len(instance_points_list) == 0:
            for instance in self.instance_list:
                distances = np.linspace(0, instance.length, self.fixed_num)
                sampled_points = np.array(
                    [list(instance.interpolate(distance).coords) for distance in distances]).reshape(
                    -1, 2)
                instance_points_list.append(sampled_points)

        instance_points_array = np.array(instance_points_list)
        instance_points_tensor = to_tensor(instance_points_array)
        instance_points_tensor = instance_points_tensor.to(
            dtype=torch.float32)
        instance_points_tensor[:, :, 0] = torch.clamp(instance_points_tensor[:, :, 0], min=-self.max_x, max=self.max_x)
        instance_points_tensor[:, :, 1] = torch.clamp(instance_points_tensor[:, :, 1], min=-self.max_y, max=self.max_y)
        return instance_points_tensor

    @property
    def fixed_num_sampled_points_ambiguity(self):
        """
        return torch.Tensor([N,fixed_num,2]), in xmin, ymin, xmax, ymax form
            N means the num of instances
        """
        assert len(self.instance_list) != 0
        instance_points_list = []
        for instance in self.instance_list:
            distances = np.linspace(0, instance.length, self.fixed_num)
            sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            instance_points_list.append(sampled_points)
        instance_points_array = np.array(instance_points_list)
        instance_points_tensor = to_tensor(instance_points_array)
        instance_points_tensor = instance_points_tensor.to(
                            dtype=torch.float32)
        instance_points_tensor[:,:,0] = torch.clamp(instance_points_tensor[:,:,0], min=-self.max_x,max=self.max_x)
        instance_points_tensor[:,:,1] = torch.clamp(instance_points_tensor[:,:,1], min=-self.max_y,max=self.max_y)
        instance_points_tensor = instance_points_tensor.unsqueeze(1)
        return instance_points_tensor

    @property
    def fixed_num_sampled_points_torch(self):
        """
        return torch.Tensor([N,fixed_num,2]), in xmin, ymin, xmax, ymax form
            N means the num of instances
        """
        assert len(self.instance_list) != 0
        instance_points_list = []
        for instance in self.instance_list:
            # distances = np.linspace(0, instance.length, self.fixed_num)
            # sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            poly_pts = to_tensor(np.array(list(instance.coords)))
            poly_pts = poly_pts.unsqueeze(0).permute(0,2,1)
            sampled_pts = torch.nn.functional.interpolate(poly_pts,size=(self.fixed_num),mode='linear',align_corners=True)
            sampled_pts = sampled_pts.permute(0,2,1).squeeze(0)
            instance_points_list.append(sampled_pts)
        # instance_points_array = np.array(instance_points_list)
        # instance_points_tensor = to_tensor(instance_points_array)
        instance_points_tensor = torch.stack(instance_points_list,dim=0)
        instance_points_tensor = instance_points_tensor.to(
                            dtype=torch.float32)
        instance_points_tensor[:,:,0] = torch.clamp(instance_points_tensor[:,:,0], min=-self.max_x,max=self.max_x)
        instance_points_tensor[:,:,1] = torch.clamp(instance_points_tensor[:,:,1], min=-self.max_y,max=self.max_y)
        return instance_points_tensor

    @property
    def shift_fixed_dist_sampled_points(self):  # TODO 추가

        assert len(self.instance_list) != 0
        instances_list = []
        for idx, instance in enumerate(self.instance_list):
            # import ipdb;ipdb.set_trace()
            instance_label = self.instance_labels[idx]
            # TODO distance 선정 방법 선택
            # 방법 1: 그냥 fixed distance에 끝점 추가 시작점 0 끝점 10 간격 3이면 0, 3, 6, 9, 10
            # distances = np.append(np.arange(0, instance.length, self.fixed_dist), instance.length)
            # 방법 2: fixed distance로 나눈 고른 간격 시작점 0 끝점 10 간격 3이면 0, 3.3333, 6.6666, 10
            if instance.length > self.fixed_dist:
                distance = instance.length / (instance.length // self.fixed_dist)
            else:
                distance = instance.length
            distances = np.append(np.arange(0, instance.length, distance), instance.length)
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            shift_pts_list = []
            pts_num, coords_num = poly_pts.shape
            shift_num = pts_num - 1
            final_shift_num = self.padding_length - 1
            if instance_label == 3:  # for centerline
                # import ipdb;ipdb.set_trace()
                sampled_points = np.array(
                    [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                sampled_points_padded = np.pad(sampled_points,
                                               ((0, self.padding_length - sampled_points.shape[0]), (0, 0)), 'constant',
                                               constant_values=self.padding_value)
                shift_pts_list.append(sampled_points_padded)
            else:
                if is_poly:
                    pts_to_shift = poly_pts[:-1, :]
                    for shift_right_i in range(shift_num):
                        shift_pts = np.roll(pts_to_shift, shift_right_i, axis=0)
                        pts_to_concat = shift_pts[0]
                        pts_to_concat = np.expand_dims(pts_to_concat, axis=0)
                        shift_pts = np.concatenate((shift_pts, pts_to_concat), axis=0)
                        shift_instance = LineString(shift_pts)
                        shift_sampled_points = np.array(
                            [list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(-1,
                                                                                                                   2)
                        shift_sampled_points_padded = np.pad(shift_sampled_points, (
                        (0, self.padding_length - shift_sampled_points.shape[0]), (0, 0)), 'constant',
                                                             constant_values=self.padding_value)
                        shift_pts_list.append(shift_sampled_points_padded)
                    # import pdb;pdb.set_trace()
                else:
                    sampled_points = np.array(
                        [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    sampled_points_padded = np.pad(sampled_points,
                                                   ((0, self.padding_length - sampled_points.shape[0]), (0, 0)),
                                                   'constant', constant_values=self.padding_value)
                    flip_sampled_points = np.flip(sampled_points, axis=0)
                    flip_sampled_points_padded = np.pad(flip_sampled_points, (
                    (0, self.padding_length - flip_sampled_points.shape[0]), (0, 0)), 'constant',
                                                        constant_values=self.padding_value)
                    shift_pts_list.append(sampled_points_padded)
                    shift_pts_list.append(flip_sampled_points_padded)

            multi_shifts_pts = np.stack(shift_pts_list, axis=0)
            shifts_num, _, _ = multi_shifts_pts.shape

            if shifts_num > final_shift_num:
                index = np.random.choice(multi_shifts_pts.shape[0], final_shift_num, replace=False)
                multi_shifts_pts = multi_shifts_pts[index]

            multi_shifts_pts_tensor = to_tensor(multi_shifts_pts)
            multi_shifts_pts_tensor = multi_shifts_pts_tensor.to(dtype=torch.float32)

            # multi_shifts_pts_tensor[:,:,0] = torch.clamp(multi_shifts_pts_tensor[:,:,0], min=-self.max_x,max=self.max_x)
            # multi_shifts_pts_tensor[:,:,1] = torch.clamp(multi_shifts_pts_tensor[:,:,1], min=-self.max_y,max=self.max_y)
            # if not is_poly:
            if multi_shifts_pts_tensor.shape[0] < final_shift_num:
                padding = torch.full([final_shift_num - multi_shifts_pts_tensor.shape[0], self.padding_length, 2],
                                     self.padding_value)
                multi_shifts_pts_tensor = torch.cat([multi_shifts_pts_tensor, padding], dim=0)
            instances_list.append(multi_shifts_pts_tensor)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(dtype=torch.float32)
        return instances_tensor

    @property
    def shift_fixed_dist_sampled_points_uni(self):  # TODO 추가

        assert len(self.instance_list) != 0
        instances_list = []
        for idx, instance in enumerate(self.instance_list):
            # import ipdb;ipdb.set_trace()
            instance_label = self.instance_labels[idx]
            if instance.length > self.fixed_dist:
                distances = np.append(np.arange(0, instance.length, self.fixed_dist), instance.length)
            else:
                distance = instance.length
            distances = np.append(np.arange(0, instance.length, distance), instance.length)
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            shift_pts_list = []
            pts_num, coords_num = poly_pts.shape
            shift_num = pts_num - 1
            final_shift_num = self.padding_length - 1
            if instance_label == 3:  # for centerline
                # import ipdb;ipdb.set_trace()
                sampled_points = np.array(
                    [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                sampled_points_padded = np.pad(sampled_points,
                                               ((0, self.padding_length - sampled_points.shape[0]), (0, 0)), 'constant',
                                               constant_values=self.padding_value)
                shift_pts_list.append(sampled_points_padded)
            else:
                if is_poly:
                    pts_to_shift = poly_pts[:-1, :]
                    for shift_right_i in range(shift_num):
                        shift_pts = np.roll(pts_to_shift, shift_right_i, axis=0)
                        pts_to_concat = shift_pts[0]
                        pts_to_concat = np.expand_dims(pts_to_concat, axis=0)
                        shift_pts = np.concatenate((shift_pts, pts_to_concat), axis=0)
                        shift_instance = LineString(shift_pts)
                        shift_sampled_points = np.array(
                            [list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(-1,
                                                                                                                   2)
                        shift_sampled_points_padded = np.pad(shift_sampled_points, (
                            (0, self.padding_length - shift_sampled_points.shape[0]), (0, 0)), 'constant',
                                                             constant_values=self.padding_value)
                        shift_pts_list.append(shift_sampled_points_padded)
                    # import pdb;pdb.set_trace()
                else:
                    sampled_points = np.array(
                        [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    sampled_points_padded = np.pad(sampled_points,
                                                   ((0, self.padding_length - sampled_points.shape[0]), (0, 0)),
                                                   'constant', constant_values=self.padding_value)
                    flip_sampled_points = np.flip(sampled_points, axis=0)
                    flip_sampled_points_padded = np.pad(flip_sampled_points, (
                        (0, self.padding_length - flip_sampled_points.shape[0]), (0, 0)), 'constant',
                                                        constant_values=self.padding_value)
                    shift_pts_list.append(sampled_points_padded)
                    shift_pts_list.append(flip_sampled_points_padded)

            multi_shifts_pts = np.stack(shift_pts_list, axis=0)
            shifts_num, _, _ = multi_shifts_pts.shape

            if shifts_num > final_shift_num:
                index = np.random.choice(multi_shifts_pts.shape[0], final_shift_num, replace=False)
                multi_shifts_pts = multi_shifts_pts[index]

            multi_shifts_pts_tensor = to_tensor(multi_shifts_pts)
            multi_shifts_pts_tensor = multi_shifts_pts_tensor.to(dtype=torch.float32)

            # multi_shifts_pts_tensor[:,:,0] = torch.clamp(multi_shifts_pts_tensor[:,:,0], min=-self.max_x,max=self.max_x)
            # multi_shifts_pts_tensor[:,:,1] = torch.clamp(multi_shifts_pts_tensor[:,:,1], min=-self.max_y,max=self.max_y)
            # if not is_poly:
            if multi_shifts_pts_tensor.shape[0] < final_shift_num:
                padding = torch.full([final_shift_num - multi_shifts_pts_tensor.shape[0], self.padding_length, 2],
                                     self.padding_value)
                multi_shifts_pts_tensor = torch.cat([multi_shifts_pts_tensor, padding], dim=0)
            instances_list.append(multi_shifts_pts_tensor)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(dtype=torch.float32)
        return instances_tensor


    @property
    def shift_fixed_num_sampled_points(self):
        """
        return  [instances_num, num_shifts, fixed_num, 2]
        """
        assert len(self.instance_list) != 0
        instances_list = []
        for instance in self.instance_list:
            distances = np.linspace(0, instance.length, self.fixed_num)
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            shift_pts_list = []
            pts_num, coords_num = poly_pts.shape
            shift_num = pts_num - 1
            final_shift_num = self.fixed_num - 1
            sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            shift_pts_list.append(sampled_points)
            # if is_poly:
            #     pts_to_shift = poly_pts[:-1,:]
            #     for shift_right_i in range(shift_num):
            #         shift_pts = np.roll(pts_to_shift,shift_right_i,axis=0)
            #         pts_to_concat = shift_pts[0]
            #         pts_to_concat = np.expand_dims(pts_to_concat,axis=0)
            #         shift_pts = np.concatenate((shift_pts,pts_to_concat),axis=0)
            #         shift_instance = LineString(shift_pts)
            #         shift_sampled_points = np.array([list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            #         shift_pts_list.append(shift_sampled_points)
            #     # import pdb;pdb.set_trace()
            # else:
            #     sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            #     flip_sampled_points = np.flip(sampled_points, axis=0)
            #     shift_pts_list.append(sampled_points)
            #     shift_pts_list.append(flip_sampled_points)
            
            multi_shifts_pts = np.stack(shift_pts_list,axis=0)
            shifts_num,_,_ = multi_shifts_pts.shape

            if shifts_num > final_shift_num:
                index = np.random.choice(multi_shifts_pts.shape[0], final_shift_num, replace=False)
                multi_shifts_pts = multi_shifts_pts[index]
            
            multi_shifts_pts_tensor = to_tensor(multi_shifts_pts)
            multi_shifts_pts_tensor = multi_shifts_pts_tensor.to(
                            dtype=torch.float32)
            
            multi_shifts_pts_tensor[:,:,0] = torch.clamp(multi_shifts_pts_tensor[:,:,0], min=-self.max_x,max=self.max_x)
            multi_shifts_pts_tensor[:,:,1] = torch.clamp(multi_shifts_pts_tensor[:,:,1], min=-self.max_y,max=self.max_y)
            # if not is_poly:
            if multi_shifts_pts_tensor.shape[0] < final_shift_num:
                padding = torch.full([final_shift_num-multi_shifts_pts_tensor.shape[0],self.fixed_num,2], self.padding_value)
                multi_shifts_pts_tensor = torch.cat([multi_shifts_pts_tensor,padding],dim=0)
            instances_list.append(multi_shifts_pts_tensor)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(
                            dtype=torch.float32)
        return instances_tensor

    @property
    def shift_fixed_num_sampled_points_v1(self):
        """
        return  [instances_num, num_shifts, fixed_num, 2]
        """
        fixed_num_sampled_points = self.fixed_num_sampled_points
        instances_list = []
        is_poly = False
        # is_line = False
        # import pdb;pdb.set_trace()
        for fixed_num_pts in fixed_num_sampled_points:
            # [fixed_num, 2]
            is_poly = fixed_num_pts[0].equal(fixed_num_pts[-1])
            pts_num = fixed_num_pts.shape[0]
            shift_num = pts_num - 1
            if is_poly:
                pts_to_shift = fixed_num_pts[:-1,:]
            shift_pts_list = []
            if is_poly:
                for shift_right_i in range(shift_num):
                    shift_pts_list.append(pts_to_shift.roll(shift_right_i,0))
            else:
                shift_pts_list.append(fixed_num_pts)
                shift_pts_list.append(fixed_num_pts.flip(0))
            shift_pts = torch.stack(shift_pts_list,dim=0)

            if is_poly:
                _, _, num_coords = shift_pts.shape
                tmp_shift_pts = shift_pts.new_zeros((shift_num, pts_num, num_coords))
                tmp_shift_pts[:,:-1,:] = shift_pts
                tmp_shift_pts[:,-1,:] = shift_pts[:,0,:]
                shift_pts = tmp_shift_pts

            shift_pts[:,:,0] = torch.clamp(shift_pts[:,:,0], min=-self.max_x,max=self.max_x)
            shift_pts[:,:,1] = torch.clamp(shift_pts[:,:,1], min=-self.max_y,max=self.max_y)

            if not is_poly:
                padding = torch.full([shift_num-shift_pts.shape[0],pts_num,2], self.padding_value)
                shift_pts = torch.cat([shift_pts,padding],dim=0)
                # padding = np.zeros((self.num_samples - len(sampled_points), 2))
                # sampled_points = np.concatenate([sampled_points, padding], axis=0)
            instances_list.append(shift_pts)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(
                            dtype=torch.float32)
        return instances_tensor

    @property
    def shift_fixed_num_sampled_points_v2(self):
        """
        return  [instances_num, num_shifts, fixed_num, 2]
        """
        assert len(self.instance_list) != 0
        instances_list = []
        for idx, instance in enumerate(self.instance_list):
            # import ipdb;ipdb.set_trace()
            instance_label = self.instance_labels[idx]
            distances = np.linspace(0, instance.length, self.fixed_num)
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            shift_pts_list = []
            pts_num, coords_num = poly_pts.shape
            shift_num = pts_num - 1
            final_shift_num = self.fixed_num - 1
            if instance_label == 3:
                # import ipdb;ipdb.set_trace()
                sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                shift_pts_list.append(sampled_points)
            else:
                if is_poly:
                    pts_to_shift = poly_pts[:-1,:]
                    for shift_right_i in range(shift_num):
                        shift_pts = np.roll(pts_to_shift,shift_right_i,axis=0)
                        pts_to_concat = shift_pts[0]
                        pts_to_concat = np.expand_dims(pts_to_concat,axis=0)
                        shift_pts = np.concatenate((shift_pts,pts_to_concat),axis=0)
                        shift_instance = LineString(shift_pts)
                        shift_sampled_points = np.array([list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                        shift_pts_list.append(shift_sampled_points)
                    # import pdb;pdb.set_trace()
                else:
                    sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    flip_sampled_points = np.flip(sampled_points, axis=0)
                    shift_pts_list.append(sampled_points)
                    shift_pts_list.append(flip_sampled_points)
            
            multi_shifts_pts = np.stack(shift_pts_list,axis=0)
            shifts_num,_,_ = multi_shifts_pts.shape

            if shifts_num > final_shift_num:
                index = np.random.choice(multi_shifts_pts.shape[0], final_shift_num, replace=False)
                multi_shifts_pts = multi_shifts_pts[index]
            
            multi_shifts_pts_tensor = to_tensor(multi_shifts_pts)
            multi_shifts_pts_tensor = multi_shifts_pts_tensor.to(
                            dtype=torch.float32)
            
            multi_shifts_pts_tensor[:,:,0] = torch.clamp(multi_shifts_pts_tensor[:,:,0], min=-self.max_x,max=self.max_x)
            multi_shifts_pts_tensor[:,:,1] = torch.clamp(multi_shifts_pts_tensor[:,:,1], min=-self.max_y,max=self.max_y)
            # if not is_poly:
            if multi_shifts_pts_tensor.shape[0] < final_shift_num:
                padding = torch.full([final_shift_num-multi_shifts_pts_tensor.shape[0],self.fixed_num,2], self.padding_value)
                multi_shifts_pts_tensor = torch.cat([multi_shifts_pts_tensor,padding],dim=0)
            instances_list.append(multi_shifts_pts_tensor)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(
                            dtype=torch.float32)
        return instances_tensor

    @property
    def shift_fixed_num_sampled_points_v2_dou(self):
        """
        return  [instances_num, num_shifts, fixed_num, 2]
        """
        assert len(self.instance_list) != 0
        instances_list = []
        for idx, instance in enumerate(self.instance_list):
            # import ipdb;ipdb.set_trace()
            instance_label = self.instance_labels[idx]
            distances = np.linspace(0, instance.length, self.fixed_num*2)
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            shift_pts_list = []
            pts_num, coords_num = poly_pts.shape
            shift_num = pts_num - 1
            final_shift_num = self.fixed_num*2 - 1
            if instance_label == 3:
                # import ipdb;ipdb.set_trace()
                sampled_points = np.array(
                    [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                shift_pts_list.append(sampled_points)
            else:
                if is_poly:
                    pts_to_shift = poly_pts[:-1, :]
                    for shift_right_i in range(shift_num):
                        shift_pts = np.roll(pts_to_shift, shift_right_i, axis=0)
                        pts_to_concat = shift_pts[0]
                        pts_to_concat = np.expand_dims(pts_to_concat, axis=0)
                        shift_pts = np.concatenate((shift_pts, pts_to_concat), axis=0)
                        shift_instance = LineString(shift_pts)
                        shift_sampled_points = np.array(
                            [list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(-1,
                                                                                                                   2)
                        shift_pts_list.append(shift_sampled_points)
                    # import pdb;pdb.set_trace()
                else:
                    sampled_points = np.array(
                        [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    flip_sampled_points = np.flip(sampled_points, axis=0)
                    shift_pts_list.append(sampled_points)
                    shift_pts_list.append(flip_sampled_points)

            multi_shifts_pts = np.stack(shift_pts_list, axis=0)
            shifts_num, _, _ = multi_shifts_pts.shape

            if shifts_num > final_shift_num:
                index = np.random.choice(multi_shifts_pts.shape[0], final_shift_num, replace=False)
                multi_shifts_pts = multi_shifts_pts[index]

            multi_shifts_pts_tensor = to_tensor(multi_shifts_pts)
            multi_shifts_pts_tensor = multi_shifts_pts_tensor.to(
                dtype=torch.float32)

            multi_shifts_pts_tensor[:, :, 0] = torch.clamp(multi_shifts_pts_tensor[:, :, 0], min=-self.max_x,
                                                           max=self.max_x)
            multi_shifts_pts_tensor[:, :, 1] = torch.clamp(multi_shifts_pts_tensor[:, :, 1], min=-self.max_y,
                                                           max=self.max_y)
            # if not is_poly:
            if multi_shifts_pts_tensor.shape[0] < final_shift_num:
                padding = torch.full([final_shift_num - multi_shifts_pts_tensor.shape[0], self.fixed_num*2, 2],
                                     self.padding_value)
                multi_shifts_pts_tensor = torch.cat([multi_shifts_pts_tensor, padding], dim=0)
            instances_list.append(multi_shifts_pts_tensor)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(
            dtype=torch.float32)
        return instances_tensor

    @property
    def shift_fixed_num_sampled_points_v2_condi(self):
        """
        return  [instances_num, num_shifts, fixed_num, 2]
        """
        assert len(self.instance_list) != 0
        instances_list = []
        for idx, instance in enumerate(self.instance_list):
            if instance.length < 2:
                continue
            # import ipdb;ipdb.set_trace()
            instance_label = self.instance_labels[idx]
            distances = np.linspace(0, instance.length, self.fixed_num)
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            shift_pts_list = []
            pts_num, coords_num = poly_pts.shape
            shift_num = pts_num - 1
            final_shift_num = self.fixed_num - 1
            if instance_label == 3:
                # import ipdb;ipdb.set_trace()
                sampled_points = np.array(
                    [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                shift_pts_list.append(sampled_points)
            else:
                if is_poly:
                    pts_to_shift = poly_pts[:-1, :]
                    for shift_right_i in range(shift_num):
                        shift_pts = np.roll(pts_to_shift, shift_right_i, axis=0)
                        pts_to_concat = shift_pts[0]
                        pts_to_concat = np.expand_dims(pts_to_concat, axis=0)
                        shift_pts = np.concatenate((shift_pts, pts_to_concat), axis=0)
                        shift_instance = LineString(shift_pts)
                        shift_sampled_points = np.array(
                            [list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(-1,
                                                                                                                   2)
                        shift_pts_list.append(shift_sampled_points)
                    # import pdb;pdb.set_trace()
                else:
                    sampled_points = np.array(
                        [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    flip_sampled_points = np.flip(sampled_points, axis=0)
                    shift_pts_list.append(sampled_points)
                    shift_pts_list.append(flip_sampled_points)

            multi_shifts_pts = np.stack(shift_pts_list, axis=0)
            shifts_num, _, _ = multi_shifts_pts.shape

            if shifts_num > final_shift_num:
                index = np.random.choice(multi_shifts_pts.shape[0], final_shift_num, replace=False)
                multi_shifts_pts = multi_shifts_pts[index]

            multi_shifts_pts_tensor = to_tensor(multi_shifts_pts)
            multi_shifts_pts_tensor = multi_shifts_pts_tensor.to(
                dtype=torch.float32)

            multi_shifts_pts_tensor[:, :, 0] = torch.clamp(multi_shifts_pts_tensor[:, :, 0], min=-self.max_x,
                                                           max=self.max_x)
            multi_shifts_pts_tensor[:, :, 1] = torch.clamp(multi_shifts_pts_tensor[:, :, 1], min=-self.max_y,
                                                           max=self.max_y)
            # if not is_poly:
            if multi_shifts_pts_tensor.shape[0] < final_shift_num:
                padding = torch.full([final_shift_num - multi_shifts_pts_tensor.shape[0], self.fixed_num, 2],
                                     self.padding_value)
                multi_shifts_pts_tensor = torch.cat([multi_shifts_pts_tensor, padding], dim=0)
            instances_list.append(multi_shifts_pts_tensor)

        if len(instances_list) == 0:
            for idx, instance in enumerate(self.instance_list):
                # import ipdb;ipdb.set_trace()
                instance_label = self.instance_labels[idx]
                distances = np.linspace(0, instance.length, self.fixed_num)
                poly_pts = np.array(list(instance.coords))
                start_pts = poly_pts[0]
                end_pts = poly_pts[-1]
                is_poly = np.equal(start_pts, end_pts)
                is_poly = is_poly.all()
                shift_pts_list = []
                pts_num, coords_num = poly_pts.shape
                shift_num = pts_num - 1
                final_shift_num = self.fixed_num - 1
                if instance_label == 3:
                    # import ipdb;ipdb.set_trace()
                    sampled_points = np.array(
                        [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    shift_pts_list.append(sampled_points)
                else:
                    if is_poly:
                        pts_to_shift = poly_pts[:-1, :]
                        for shift_right_i in range(shift_num):
                            shift_pts = np.roll(pts_to_shift, shift_right_i, axis=0)
                            pts_to_concat = shift_pts[0]
                            pts_to_concat = np.expand_dims(pts_to_concat, axis=0)
                            shift_pts = np.concatenate((shift_pts, pts_to_concat), axis=0)
                            shift_instance = LineString(shift_pts)
                            shift_sampled_points = np.array(
                                [list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(
                                -1,
                                2)
                            shift_pts_list.append(shift_sampled_points)
                        # import pdb;pdb.set_trace()
                    else:
                        sampled_points = np.array(
                            [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                        flip_sampled_points = np.flip(sampled_points, axis=0)
                        shift_pts_list.append(sampled_points)
                        shift_pts_list.append(flip_sampled_points)

                multi_shifts_pts = np.stack(shift_pts_list, axis=0)
                shifts_num, _, _ = multi_shifts_pts.shape

                if shifts_num > final_shift_num:
                    index = np.random.choice(multi_shifts_pts.shape[0], final_shift_num, replace=False)
                    multi_shifts_pts = multi_shifts_pts[index]

                multi_shifts_pts_tensor = to_tensor(multi_shifts_pts)
                multi_shifts_pts_tensor = multi_shifts_pts_tensor.to(
                    dtype=torch.float32)

                multi_shifts_pts_tensor[:, :, 0] = torch.clamp(multi_shifts_pts_tensor[:, :, 0], min=-self.max_x,
                                                               max=self.max_x)
                multi_shifts_pts_tensor[:, :, 1] = torch.clamp(multi_shifts_pts_tensor[:, :, 1], min=-self.max_y,
                                                               max=self.max_y)
                # if not is_poly:
                if multi_shifts_pts_tensor.shape[0] < final_shift_num:
                    padding = torch.full([final_shift_num - multi_shifts_pts_tensor.shape[0], self.fixed_num, 2],
                                         self.padding_value)
                    multi_shifts_pts_tensor = torch.cat([multi_shifts_pts_tensor, padding], dim=0)
                instances_list.append(multi_shifts_pts_tensor)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(
            dtype=torch.float32)
        return instances_tensor

    @property
    def shift_fixed_num_sampled_points_v3(self):
        """
        return  [instances_num, num_shifts, fixed_num, 2]
        """
        assert len(self.instance_list) != 0
        instances_list = []
        for instance in self.instance_list:
            distances = np.linspace(0, instance.length, self.fixed_num)
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            shift_pts_list = []
            pts_num, coords_num = poly_pts.shape
            shift_num = pts_num - 1
            final_shift_num = self.fixed_num - 1
            if is_poly:
                pts_to_shift = poly_pts[:-1,:]
                for shift_right_i in range(shift_num):
                    shift_pts = np.roll(pts_to_shift,shift_right_i,axis=0)
                    pts_to_concat = shift_pts[0]
                    pts_to_concat = np.expand_dims(pts_to_concat,axis=0)
                    shift_pts = np.concatenate((shift_pts,pts_to_concat),axis=0)
                    shift_instance = LineString(shift_pts)
                    shift_sampled_points = np.array([list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    shift_pts_list.append(shift_sampled_points)
                flip_pts_to_shift = np.flip(pts_to_shift, axis=0)
                for shift_right_i in range(shift_num):
                    shift_pts = np.roll(flip_pts_to_shift,shift_right_i,axis=0)
                    pts_to_concat = shift_pts[0]
                    pts_to_concat = np.expand_dims(pts_to_concat,axis=0)
                    shift_pts = np.concatenate((shift_pts,pts_to_concat),axis=0)
                    shift_instance = LineString(shift_pts)
                    shift_sampled_points = np.array([list(shift_instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    shift_pts_list.append(shift_sampled_points)
            else:
                sampled_points = np.array([list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                flip_sampled_points = np.flip(sampled_points, axis=0)
                shift_pts_list.append(sampled_points)
                shift_pts_list.append(flip_sampled_points)
            
            multi_shifts_pts = np.stack(shift_pts_list,axis=0)
            shifts_num,_,_ = multi_shifts_pts.shape
            if shifts_num > 2*final_shift_num:
                index = np.random.choice(shift_num, final_shift_num, replace=False)
                flip0_shifts_pts = multi_shifts_pts[index]
                flip1_shifts_pts = multi_shifts_pts[index+shift_num]
                multi_shifts_pts = np.concatenate((flip0_shifts_pts,flip1_shifts_pts),axis=0)
            
            multi_shifts_pts_tensor = to_tensor(multi_shifts_pts)
            multi_shifts_pts_tensor = multi_shifts_pts_tensor.to(
                            dtype=torch.float32)
            
            multi_shifts_pts_tensor[:,:,0] = torch.clamp(multi_shifts_pts_tensor[:,:,0], min=-self.max_x,max=self.max_x)
            multi_shifts_pts_tensor[:,:,1] = torch.clamp(multi_shifts_pts_tensor[:,:,1], min=-self.max_y,max=self.max_y)
            if multi_shifts_pts_tensor.shape[0] < 2*final_shift_num:
                padding = torch.full([final_shift_num*2-multi_shifts_pts_tensor.shape[0],self.fixed_num,2], self.padding_value)
                multi_shifts_pts_tensor = torch.cat([multi_shifts_pts_tensor,padding],dim=0)
            instances_list.append(multi_shifts_pts_tensor)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(
                            dtype=torch.float32)
        return instances_tensor

    @property
    def shift_fixed_num_sampled_points_v4(self):
        """
        return  [instances_num, num_shifts, fixed_num, 2]
        """
        fixed_num_sampled_points = self.fixed_num_sampled_points
        instances_list = []
        is_poly = False
        for fixed_num_pts in fixed_num_sampled_points:
            is_poly = fixed_num_pts[0].equal(fixed_num_pts[-1])
            pts_num = fixed_num_pts.shape[0]
            shift_num = pts_num - 1
            shift_pts_list = []
            if is_poly:
                pts_to_shift = fixed_num_pts[:-1,:]
                for shift_right_i in range(shift_num):
                    shift_pts_list.append(pts_to_shift.roll(shift_right_i,0))
                flip_pts_to_shift = pts_to_shift.flip(0)
                for shift_right_i in range(shift_num):
                    shift_pts_list.append(flip_pts_to_shift.roll(shift_right_i,0))
            else:
                shift_pts_list.append(fixed_num_pts)
                shift_pts_list.append(fixed_num_pts.flip(0))
            shift_pts = torch.stack(shift_pts_list,dim=0)

            if is_poly:
                _, _, num_coords = shift_pts.shape
                tmp_shift_pts = shift_pts.new_zeros((shift_num*2, pts_num, num_coords))
                tmp_shift_pts[:,:-1,:] = shift_pts
                tmp_shift_pts[:,-1,:] = shift_pts[:,0,:]
                shift_pts = tmp_shift_pts

            shift_pts[:,:,0] = torch.clamp(shift_pts[:,:,0], min=-self.max_x,max=self.max_x)
            shift_pts[:,:,1] = torch.clamp(shift_pts[:,:,1], min=-self.max_y,max=self.max_y)

            if not is_poly:
                padding = torch.full([shift_num*2-shift_pts.shape[0],pts_num,2], self.padding_value)
                shift_pts = torch.cat([shift_pts,padding],dim=0)
            instances_list.append(shift_pts)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(
                            dtype=torch.float32)
        return instances_tensor

    @property
    def shift_fixed_num_sampled_points_torch(self):
        """
        return  [instances_num, num_shifts, fixed_num, 2]
        """
        fixed_num_sampled_points = self.fixed_num_sampled_points_torch
        instances_list = []
        is_poly = False

        for fixed_num_pts in fixed_num_sampled_points:
            is_poly = fixed_num_pts[0].equal(fixed_num_pts[-1])
            fixed_num = fixed_num_pts.shape[0]
            shift_pts_list = []
            if is_poly:
                for shift_right_i in range(fixed_num):
                    shift_pts_list.append(fixed_num_pts.roll(shift_right_i,0))
            else:
                shift_pts_list.append(fixed_num_pts)
                shift_pts_list.append(fixed_num_pts.flip(0))
            shift_pts = torch.stack(shift_pts_list,dim=0)

            shift_pts[:,:,0] = torch.clamp(shift_pts[:,:,0], min=-self.max_x,max=self.max_x)
            shift_pts[:,:,1] = torch.clamp(shift_pts[:,:,1], min=-self.max_y,max=self.max_y)

            if not is_poly:
                padding = torch.full([fixed_num-shift_pts.shape[0],fixed_num,2], self.padding_value)
                shift_pts = torch.cat([shift_pts,padding],dim=0)
            instances_list.append(shift_pts)
        instances_tensor = torch.stack(instances_list, dim=0)
        instances_tensor = instances_tensor.to(
                            dtype=torch.float32)
        return instances_tensor

    # TODO mask #
    @property
    def instance_segments(self):

        assert len(self.instance_list) != 0
        instance_points_list = []
        self.mask_size = [200, 100]  # TODO simple bev
        self.scale_y = self.mask_size[0] / self.patch_size[0]
        self.scale_x = self.mask_size[1] / self.patch_size[1]
        instance_segm_list = []
        for instance in self.instance_list:
            distances = np.arange(0, instance.length, self.sample_dist)
            # HD map gt가 fixed로 선언되어 있어 fixed로 하려다가 1 phase에서는 segmentation만 진행하니까 왜곡이 적은게 낫지 않을까 하여 dist로 했습니다
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            sampled_points = np.array(
                [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            instance_points_list.append(sampled_points)
        for instance in instance_points_list:
            instance_segm = np.zeros((self.mask_size[0], self.mask_size[1]), dtype=np.uint8)
            try:
                self.line_ego_to_mask(LineString(instance), instance_segm, color=1, thickness=3)
            except:
                pass
            instance_segm_list.append(instance_segm)

        instance_segm_tensor = to_tensor(instance_segm_list)
        return instance_segm_tensor

    @property
    def instance_segments_condi(self):

        assert len(self.instance_list) != 0
        instance_points_list = []
        self.mask_size = [200, 100]  # TODO simple bev
        self.scale_y = self.mask_size[0] / self.patch_size[0]
        self.scale_x = self.mask_size[1] / self.patch_size[1]
        instance_segm_list = []
        for instance in self.instance_list:
            if instance.length < 2:
                continue
            distances = np.arange(0, instance.length, self.sample_dist)
            # HD map gt가 fixed로 선언되어 있어 fixed로 하려다가 1 phase에서는 segmentation만 진행하니까 왜곡이 적은게 낫지 않을까 하여 dist로 했습니다
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            sampled_points = np.array(
                [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            instance_points_list.append(sampled_points)

        if len(instance_points_list) == 0:
            for instance in self.instance_list:
                distances = np.arange(0, instance.length, self.sample_dist)
                # HD map gt가 fixed로 선언되어 있어 fixed로 하려다가 1 phase에서는 segmentation만 진행하니까 왜곡이 적은게 낫지 않을까 하여 dist로 했습니다
                poly_pts = np.array(list(instance.coords))
                start_pts = poly_pts[0]
                end_pts = poly_pts[-1]
                is_poly = np.equal(start_pts, end_pts)
                is_poly = is_poly.all()
                sampled_points = np.array(
                    [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                instance_points_list.append(sampled_points)

        for instance in instance_points_list:
            instance_segm = np.zeros((self.mask_size[0], self.mask_size[1]), dtype=np.uint8)
            try:
                self.line_ego_to_mask(LineString(instance), instance_segm, color=1, thickness=3)
            except:
                pass
            instance_segm_list.append(instance_segm)

        instance_segm_tensor = to_tensor(instance_segm_list)
        return instance_segm_tensor

    @property
    def instance_segments_v2(self):

        assert len(self.instance_list) != 0
        instance_points_list = []
        self.mask_size = [200, 100]  # TODO simple bev
        self.scale_y = self.mask_size[0] / self.patch_size[0]
        self.scale_x = self.mask_size[1] / self.patch_size[1]
        instance_segm_list = []
        for instance in self.instance_list:
            distances = np.arange(0, instance.length, self.sample_dist)
            # HD map gt가 fixed로 선언되어 있어 fixed로 하려다가 1 phase에서는 segmentation만 진행하니까 왜곡이 적은게 낫지 않을까 하여 dist로 했습니다
            poly_pts = np.array(list(instance.coords))
            start_pts = poly_pts[0]
            end_pts = poly_pts[-1]
            is_poly = np.equal(start_pts, end_pts)
            is_poly = is_poly.all()
            sampled_points = np.array(
                [list(instance.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
            instance_points_list.append(sampled_points)
        for instance in instance_points_list:
            instance_segm = np.zeros((self.mask_size[0], self.mask_size[1]), dtype=np.uint8)
            try:
                self.line_ego_to_mask(LineString(instance), instance_segm, color=1, thickness=1)
            except:
                pass
            instance_segm_list.append(instance_segm)

        instance_segm_tensor = to_tensor(instance_segm_list)
        return instance_segm_tensor
# TODO mask ##


class VectorizedLocalMap(object):
    CLASS2LABEL = {
        'divider': 0,
        'ped_crossing': 1,
        'boundary': 2,
        'centerline': 3,
        'others': -1
    }
    def __init__(self,
                 canvas_size, 
                 patch_size,
                 map_classes=['divider','ped_crossing','boundary'],
                 sample_dist=1,
                 num_samples=250,
                 padding=False,
                 fixed_ptsnum_per_line=-1,
                 Ext_fixed_ptsnum_per_line=-1,
                 padding_value=-10000,
                 thickness=3,
                 aux_seg = dict(
                    use_aux_seg=False,
                    bev_seg=False,
                    pv_seg=False,
                    seg_classes=1,
                    feat_down_sample=32)):
        '''
        Args:
            fixed_ptsnum_per_line = -1 : no fixed num
        '''
        super().__init__()

        self.vec_classes = map_classes


        self.sample_dist = sample_dist
        self.num_samples = num_samples
        self.padding = padding
        self.fixed_num = fixed_ptsnum_per_line
        self.Ext_fixed_num = Ext_fixed_ptsnum_per_line
        self.padding_value = padding_value

        # for semantic mask
        self.patch_size = patch_size
        self.canvas_size = canvas_size
        self.thickness = thickness
        self.scale_x = self.canvas_size[1] / self.patch_size[1]
        self.scale_y = self.canvas_size[0] / self.patch_size[0]
        # self.auxseg_use_sem = auxseg_use_sem
        self.aux_seg = aux_seg

    def gen_vectorized_samples(self, map_annotation, example=None, feat_down_sample=32):
        '''
        use lidar2global to get gt map layers
        '''
        vectors = []
        for vec_class in self.vec_classes:
            instance_list = map_annotation[vec_class]
            for instance in instance_list:
                vectors.append((LineString(np.array(instance)), self.CLASS2LABEL.get(vec_class, -1))) 
        # import pdb;pdb.set_trace()
        filtered_vectors = []
        gt_pts_loc_3d = []
        gt_pts_num_3d = []
        gt_labels = []
        gt_instance = []
        if self.aux_seg['use_aux_seg']:
            if self.aux_seg['seg_classes'] == 1:
                gt_semantic_mask = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)

                # import ipdb;ipdb.set_trace()
                if self.aux_seg['pv_seg']:
                    num_cam  = len(example['img_metas'].data['pad_shape'])
                    img_shape = example['img_metas'].data['pad_shape'][0]
                    # import ipdb;ipdb.set_trace()
                    gt_pv_semantic_mask = np.zeros((num_cam, 1, img_shape[0] // feat_down_sample, img_shape[1] // feat_down_sample), dtype=np.uint8)
                    lidar2img = example['img_metas'].data['lidar2img']
                    scale_factor = np.eye(4)
                    scale_factor[0, 0] *= 1/32
                    scale_factor[1, 1] *= 1/32
                    lidar2feat = [scale_factor @ l2i for l2i in lidar2img]
                else:
                    gt_pv_semantic_mask = None
                for instance, instance_type in vectors:
                    if instance_type != -1:
                        gt_instance.append(instance)
                        gt_labels.append(instance_type)
                        if instance.geom_type == 'LineString':
                            self.line_ego_to_mask(instance, gt_semantic_mask[0], color=1, thickness=self.thickness)
                            if self.aux_seg['pv_seg']:
                                for cam_index in range(num_cam):
                                    self.line_ego_to_pvmask(instance, gt_pv_semantic_mask[cam_index][0], lidar2feat[cam_index],color=1, thickness=self.aux_seg['pv_thickness'])
                        else:
                            print(instance.geom_type)
            else:
                gt_semantic_mask = np.zeros((len(self.vec_classes), self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)

                if self.aux_seg['pv_seg']:
                    num_cam  = len(example['img_metas'].data['pad_shape'])
                    gt_pv_semantic_mask = np.zeros((num_cam, len(self.vec_classes), img_shape[0] // feat_down_sample, img_shape[1] // feat_down_sample), dtype=np.uint8)
                    lidar2img = example['img_metas'].data['lidar2img']
                    scale_factor = np.eye(4)
                    scale_factor[0, 0] *= 1/32
                    scale_factor[1, 1] *= 1/32
                    lidar2feat = [scale_factor @ l2i for l2i in lidar2img]
                else:
                    gt_pv_semantic_mask = None
                for instance, instance_type in vectors:
                    if instance_type != -1:
                        gt_instance.append(instance)
                        gt_labels.append(instance_type)
                        if instance.geom_type == 'LineString':
                            self.line_ego_to_mask(instance, gt_semantic_mask[instance_type], color=1, thickness=self.thickness)
                            if self.aux_seg['pv_seg']:
                                for cam_index in range(num_cam):
                                    self.line_ego_to_pvmask(instance, gt_pv_semantic_mask[cam_index][instance_type], lidar2feat[cam_index],color=1, thickness=self.aux_seg['pv_thickness'])
                        else:
                            print(instance.geom_type)
        else:
            for instance, instance_type in vectors:
                if instance_type != -1:
                    gt_instance.append(instance)
                    gt_labels.append(instance_type)
            gt_semantic_mask=None
            gt_pv_semantic_mask=None
        gt_instance = LiDARInstanceLines(gt_instance,gt_labels, self.sample_dist,
                        self.num_samples, self.padding, self.fixed_num, self.Ext_fixed_num,self.padding_value, patch_size=self.patch_size)


        anns_results = dict(
            gt_vecs_pts_loc=gt_instance,
            gt_vecs_label=gt_labels,
            gt_semantic_mask=gt_semantic_mask,
            gt_pv_semantic_mask=gt_pv_semantic_mask,
        )
        return anns_results
    def line_ego_to_pvmask(self,
                          line_ego, 
                          mask, 
                          lidar2feat,
                          color=1, 
                          thickness=1,
                          z=-1.6):
        distances = np.linspace(0, line_ego.length, 200)
        coords = np.array([list(line_ego.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
        pts_num = coords.shape[0]
        zeros = np.zeros((pts_num,1))
        zeros[:] = z
        ones = np.ones((pts_num,1))
        lidar_coords = np.concatenate([coords,zeros,ones], axis=1).transpose(1,0)
        pix_coords = perspective(lidar_coords, lidar2feat)
        cv2.polylines(mask, np.int32([pix_coords]), False, color=color, thickness=thickness)
        
    def line_ego_to_mask(self, 
                         line_ego, 
                         mask, 
                         color=1, 
                         thickness=3):
        ''' Rasterize a single line to mask.
        
        Args:
            line_ego (LineString): line
            mask (array): semantic mask to paint on
            color (int): positive label, default: 1
            thickness (int): thickness of rasterized lines, default: 3
        '''

        trans_x = self.canvas_size[1] / 2
        trans_y = self.canvas_size[0] / 2
        line_ego = affinity.scale(line_ego, self.scale_x, self.scale_y, origin=(0, 0))
        line_ego = affinity.affine_transform(line_ego, [1.0, 0.0, 0.0, 1.0, trans_x, trans_y])
        # print(np.array(list(line_ego.coords), dtype=np.int32).shape)
        coords = np.array(list(line_ego.coords), dtype=np.int32)[:, :2]
        coords = coords.reshape((-1, 2))
        assert len(coords) >= 2
        
        cv2.polylines(mask, np.int32([coords]), False, color=color, thickness=thickness)

    def get_map_geom(self, patch_box, patch_angle, layer_names, location):
        map_geom = []
        for layer_name in layer_names:
            if layer_name in self.line_classes:
                geoms = self.get_divider_line(patch_box, patch_angle, layer_name, location)
                map_geom.append((layer_name, geoms))
            elif layer_name in self.polygon_classes:
                geoms = self.get_contour_line(patch_box, patch_angle, layer_name, location)
                map_geom.append((layer_name, geoms))
            elif layer_name in self.ped_crossing_classes:
                geoms = self.get_ped_crossing_line(patch_box, patch_angle, location)
                map_geom.append((layer_name, geoms))
        return map_geom

    def _one_type_line_geom_to_vectors(self, line_geom):
        line_vectors = []
        
        for line in line_geom:
            if not line.is_empty:
                if line.geom_type == 'MultiLineString':
                    for single_line in line.geoms:
                        line_vectors.append(self.sample_pts_from_line(single_line))
                elif line.geom_type == 'LineString':
                    line_vectors.append(self.sample_pts_from_line(line))
                else:
                    raise NotImplementedError
        return line_vectors

    def _one_type_line_geom_to_instances(self, line_geom):
        line_instances = []
        
        for line in line_geom:
            if not line.is_empty:
                if line.geom_type == 'MultiLineString':
                    for single_line in line.geoms:
                        line_instances.append(single_line)
                elif line.geom_type == 'LineString':
                    line_instances.append(line)
                else:
                    raise NotImplementedError
        return line_instances

    def poly_geoms_to_vectors(self, polygon_geom):
        roads = polygon_geom[0][1]
        lanes = polygon_geom[1][1]
        union_roads = ops.unary_union(roads)
        union_lanes = ops.unary_union(lanes)
        union_segments = ops.unary_union([union_roads, union_lanes])
        max_x = self.patch_size[1] / 2
        max_y = self.patch_size[0] / 2
        local_patch = box(-max_x + 0.2, -max_y + 0.2, max_x - 0.2, max_y - 0.2)
        exteriors = []
        interiors = []
        if union_segments.geom_type != 'MultiPolygon':
            union_segments = MultiPolygon([union_segments])
        for poly in union_segments.geoms:
            exteriors.append(poly.exterior)
            for inter in poly.interiors:
                interiors.append(inter)

        results = []
        for ext in exteriors:
            if ext.is_ccw:
                ext.coords = list(ext.coords)[::-1]
            lines = ext.intersection(local_patch)
            if isinstance(lines, MultiLineString):
                lines = ops.linemerge(lines)
            results.append(lines)

        for inter in interiors:
            if not inter.is_ccw:
                inter.coords = list(inter.coords)[::-1]
            lines = inter.intersection(local_patch)
            if isinstance(lines, MultiLineString):
                lines = ops.linemerge(lines)
            results.append(lines)

        return self._one_type_line_geom_to_vectors(results)

    def ped_poly_geoms_to_instances(self, ped_geom):
        ped = ped_geom[0][1]
        union_segments = ops.unary_union(ped)
        max_x = self.patch_size[1] / 2
        max_y = self.patch_size[0] / 2
        local_patch = box(-max_x - 0.2, -max_y - 0.2, max_x + 0.2, max_y + 0.2)
        exteriors = []
        interiors = []
        if union_segments.geom_type != 'MultiPolygon':
            union_segments = MultiPolygon([union_segments])
        for poly in union_segments.geoms:
            exteriors.append(poly.exterior)
            for inter in poly.interiors:
                interiors.append(inter)

        results = []
        for ext in exteriors:
            if ext.is_ccw:
                ext.coords = list(ext.coords)[::-1]
            lines = ext.intersection(local_patch)
            if isinstance(lines, MultiLineString):
                lines = ops.linemerge(lines)
            results.append(lines)

        for inter in interiors:
            if not inter.is_ccw:
                inter.coords = list(inter.coords)[::-1]
            lines = inter.intersection(local_patch)
            if isinstance(lines, MultiLineString):
                lines = ops.linemerge(lines)
            results.append(lines)

        return self._one_type_line_geom_to_instances(results)


    def poly_geoms_to_instances(self, polygon_geom):
        roads = polygon_geom[0][1]
        lanes = polygon_geom[1][1]
        union_roads = ops.unary_union(roads)
        union_lanes = ops.unary_union(lanes)
        union_segments = ops.unary_union([union_roads, union_lanes])
        max_x = self.patch_size[1] / 2
        max_y = self.patch_size[0] / 2
        local_patch = box(-max_x + 0.2, -max_y + 0.2, max_x - 0.2, max_y - 0.2)
        exteriors = []
        interiors = []
        if union_segments.geom_type != 'MultiPolygon':
            union_segments = MultiPolygon([union_segments])
        for poly in union_segments.geoms:
            exteriors.append(poly.exterior)
            for inter in poly.interiors:
                interiors.append(inter)

        results = []
        for ext in exteriors:
            if ext.is_ccw:
                ext.coords = list(ext.coords)[::-1]
            lines = ext.intersection(local_patch)
            if isinstance(lines, MultiLineString):
                lines = ops.linemerge(lines)
            results.append(lines)

        for inter in interiors:
            if not inter.is_ccw:
                inter.coords = list(inter.coords)[::-1]
            lines = inter.intersection(local_patch)
            if isinstance(lines, MultiLineString):
                lines = ops.linemerge(lines)
            results.append(lines)

        return self._one_type_line_geom_to_instances(results)

    def line_geoms_to_vectors(self, line_geom):
        line_vectors_dict = dict()
        for line_type, a_type_of_lines in line_geom:
            one_type_vectors = self._one_type_line_geom_to_vectors(a_type_of_lines)
            line_vectors_dict[line_type] = one_type_vectors

        return line_vectors_dict
    def line_geoms_to_instances(self, line_geom):
        line_instances_dict = dict()
        for line_type, a_type_of_lines in line_geom:
            one_type_instances = self._one_type_line_geom_to_instances(a_type_of_lines)
            line_instances_dict[line_type] = one_type_instances

        return line_instances_dict

    def ped_geoms_to_vectors(self, ped_geom):
        ped_geom = ped_geom[0][1]
        union_ped = ops.unary_union(ped_geom)
        if union_ped.geom_type != 'MultiPolygon':
            union_ped = MultiPolygon([union_ped])

        max_x = self.patch_size[1] / 2
        max_y = self.patch_size[0] / 2
        local_patch = box(-max_x + 0.2, -max_y + 0.2, max_x - 0.2, max_y - 0.2)
        results = []
        for ped_poly in union_ped:
            # rect = ped_poly.minimum_rotated_rectangle
            ext = ped_poly.exterior
            if not ext.is_ccw:
                ext.coords = list(ext.coords)[::-1]
            lines = ext.intersection(local_patch)
            results.append(lines)

        return self._one_type_line_geom_to_vectors(results)

    def get_contour_line(self,patch_box,patch_angle,layer_name,location):
        if layer_name not in self.map_explorer[location].map_api.non_geometric_polygon_layers:
            raise ValueError('{} is not a polygonal layer'.format(layer_name))

        patch_x = patch_box[0]
        patch_y = patch_box[1]

        patch = self.map_explorer[location].get_patch_coord(patch_box, patch_angle)

        records = getattr(self.map_explorer[location].map_api, layer_name)

        polygon_list = []
        if layer_name == 'drivable_area':
            for record in records:
                polygons = [self.map_explorer[location].map_api.extract_polygon(polygon_token) for polygon_token in record['polygon_tokens']]

                for polygon in polygons:
                    new_polygon = polygon.intersection(patch)
                    if not new_polygon.is_empty:
                        new_polygon = affinity.rotate(new_polygon, -patch_angle,
                                                      origin=(patch_x, patch_y), use_radians=False)
                        new_polygon = affinity.affine_transform(new_polygon,
                                                                [1.0, 0.0, 0.0, 1.0, -patch_x, -patch_y])
                        if new_polygon.geom_type == 'Polygon':
                            new_polygon = MultiPolygon([new_polygon])
                        polygon_list.append(new_polygon)

        else:
            for record in records:
                polygon = self.map_explorer[location].map_api.extract_polygon(record['polygon_token'])

                if polygon.is_valid:
                    new_polygon = polygon.intersection(patch)
                    if not new_polygon.is_empty:
                        new_polygon = affinity.rotate(new_polygon, -patch_angle,
                                                      origin=(patch_x, patch_y), use_radians=False)
                        new_polygon = affinity.affine_transform(new_polygon,
                                                                [1.0, 0.0, 0.0, 1.0, -patch_x, -patch_y])
                        if new_polygon.geom_type == 'Polygon':
                            new_polygon = MultiPolygon([new_polygon])
                        polygon_list.append(new_polygon)

        return polygon_list

    def get_divider_line(self,patch_box,patch_angle,layer_name,location):
        if layer_name not in self.map_explorer[location].map_api.non_geometric_line_layers:
            raise ValueError("{} is not a line layer".format(layer_name))

        if layer_name == 'traffic_light':
            return None

        patch_x = patch_box[0]
        patch_y = patch_box[1]

        patch = self.map_explorer[location].get_patch_coord(patch_box, patch_angle)

        line_list = []
        records = getattr(self.map_explorer[location].map_api, layer_name)
        for record in records:
            line = self.map_explorer[location].map_api.extract_line(record['line_token'])
            if line.is_empty:  # Skip lines without nodes.
                continue

            new_line = line.intersection(patch)
            if not new_line.is_empty:
                new_line = affinity.rotate(new_line, -patch_angle, origin=(patch_x, patch_y), use_radians=False)
                new_line = affinity.affine_transform(new_line,
                                                     [1.0, 0.0, 0.0, 1.0, -patch_x, -patch_y])
                line_list.append(new_line)

        return line_list

    def get_ped_crossing_line(self, patch_box, patch_angle, location):
        patch_x = patch_box[0]
        patch_y = patch_box[1]

        patch = self.map_explorer[location].get_patch_coord(patch_box, patch_angle)
        polygon_list = []
        records = getattr(self.map_explorer[location].map_api, 'ped_crossing')
        # records = getattr(self.nusc_maps[location], 'ped_crossing')
        for record in records:
            polygon = self.map_explorer[location].map_api.extract_polygon(record['polygon_token'])
            if polygon.is_valid:
                new_polygon = polygon.intersection(patch)
                if not new_polygon.is_empty:
                    new_polygon = affinity.rotate(new_polygon, -patch_angle,
                                                      origin=(patch_x, patch_y), use_radians=False)
                    new_polygon = affinity.affine_transform(new_polygon,
                                                            [1.0, 0.0, 0.0, 1.0, -patch_x, -patch_y])
                    if new_polygon.geom_type == 'Polygon':
                        new_polygon = MultiPolygon([new_polygon])
                    polygon_list.append(new_polygon)

        return polygon_list

    def sample_pts_from_line(self, line):
        if self.fixed_num < 0:
            distances = np.arange(0, line.length, self.sample_dist)
            sampled_points = np.array([list(line.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
        else:
            # fixed number of points, so distance is line.length / self.fixed_num
            distances = np.linspace(0, line.length, self.fixed_num)
            sampled_points = np.array([list(line.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)


        num_valid = len(sampled_points)

        if not self.padding or self.fixed_num > 0:
            return sampled_points, num_valid

        # fixed distance sampling need padding!
        num_valid = len(sampled_points)

        if self.fixed_num < 0:
            if num_valid < self.num_samples:
                padding = np.zeros((self.num_samples - len(sampled_points), 2))
                sampled_points = np.concatenate([sampled_points, padding], axis=0)
            else:
                sampled_points = sampled_points[:self.num_samples, :]
                num_valid = self.num_samples


        return sampled_points, num_valid


@DATASETS.register_module()
class CustomNuScenesOfflineLocalMapDataset(CustomNuScenesDataset):
    r"""NuScenes Dataset.

    This datset add static map elements
    """
    MAPCLASSES = ('divider',)
    def __init__(self,
                 map_ann_file=None, 
                 queue_length=4, 
                 bev_size=(200, 200), 
                 pc_range=[-51.2, -51.2, -5.0, 51.2, 51.2, 3.0],
                 overlap_test=False, 
                 fixed_ptsnum_per_line=-1,
                 Ext_fixed_ptsnum_per_line=-1,
                 eval_use_same_gt_sample_num_flag=False,
                 padding_value=-10000,
                 map_classes=None,
                 noise='None',
                 noise_std=0,
                 pts_thr=0.8,
                 aux_seg = dict(
                    use_aux_seg=False,
                    bev_seg=False,
                    pv_seg=False,
                    seg_classes=1,
                    feat_down_sample=32,
                 ),
                 mini_train = False,
                 sort_by_scene=False,
                 use_sequence_group_flag=False,  # TODO multi gpu
                 sequences_split_num=1,  # TODO multi gpu
                 dn_enabled=False,  # TODO dn
                 *args, 
                 **kwargs):
        self.mini_train = mini_train
        self.sort_by_scene = sort_by_scene
        super().__init__(*args, **kwargs)
        self.map_ann_file = map_ann_file

        self.queue_length = queue_length
        self.overlap_test = overlap_test

        self.bev_size = bev_size
        self.MAPCLASSES = self.get_map_classes(map_classes)
        self.NUM_MAPCLASSES = len(self.MAPCLASSES)
        self.pc_range = pc_range
        patch_h = pc_range[4]-pc_range[1]
        patch_w = pc_range[3]-pc_range[0]
        self.patch_size = (patch_h, patch_w)
        self.padding_value = padding_value
        self.fixed_num = fixed_ptsnum_per_line
        self.eval_use_same_gt_sample_num_flag = eval_use_same_gt_sample_num_flag
        self.aux_seg = aux_seg
        self.pts_thr = pts_thr
        self.vector_map = VectorizedLocalMap(canvas_size=bev_size,
                                             patch_size=self.patch_size, 
                                             map_classes=self.MAPCLASSES, 
                                             fixed_ptsnum_per_line=fixed_ptsnum_per_line,
                                             Ext_fixed_ptsnum_per_line=Ext_fixed_ptsnum_per_line,
                                             padding_value=self.padding_value,
                                             aux_seg=aux_seg)
        self.is_vis_on_test = False
        self.noise = noise
        self.noise_std = noise_std
        # TODO multi gpu
        self.use_sequence_group_flag = use_sequence_group_flag
        self.sequences_split_num = sequences_split_num
        # TODO sort_by_scene #
        if self.sort_by_scene:
            # sequences_split_num splits each sequence into sequences_split_num parts.
            if self.test_mode:
                assert self.sequences_split_num == 1
            if self.use_sequence_group_flag:
                self._set_sequence_group_flag()
        self.dn_enabled = dn_enabled  # TODO dn

    @classmethod
    def get_map_classes(cls, map_classes=None):
        """Get class names of current dataset.

        Args:
            classes (Sequence[str] | str | None): If classes is None, use
                default CLASSES defined by builtin dataset. If classes is a
                string, take it as a file name. The file contains the name of
                classes where each line contains one class name. If classes is
                a tuple or list, override the CLASSES defined by the dataset.

        Return:
            list[str]: A list of class names.
        """
        if map_classes is None:
            return cls.MAPCLASSES

        if isinstance(map_classes, str):
            # take it as a file path
            class_names = mmcv.list_from_file(map_classes)
        elif isinstance(map_classes, (tuple, list)):
            class_names = map_classes
        else:
            raise ValueError(f'Unsupported type {type(map_classes)} of map classes.')

        return class_names

    def _set_sequence_group_flag(self):  # TODO multi gpu
        """
        Set each sequence to be a different group
        """
        res = []

        curr_sequence = 0
        for idx in range(len(self.data_infos)):
            if idx != 0 and len(self.data_infos[idx]['sweeps']) == 0:
                # Not first frame and # of sweeps is 0 -> new sequence
                curr_sequence += 1
            res.append(curr_sequence)
        self.flag = np.array(res, dtype=np.int64)
        if self.sequences_split_num != 1:
            if self.sequences_split_num == 'all':
                self.flag = np.array(range(len(self.data_infos)), dtype=np.int64)
            else:
                bin_counts = np.bincount(self.flag)
                new_flags = []
                curr_new_flag = 0
                for curr_flag in range(len(bin_counts)):
                    curr_sequence_length = np.array(
                        list(range(0,
                                   bin_counts[curr_flag],
                                   math.ceil(bin_counts[curr_flag] / self.sequences_split_num)))
                        + [bin_counts[curr_flag]])

                    for sub_seq_idx in (curr_sequence_length[1:] - curr_sequence_length[:-1]):
                        for _ in range(sub_seq_idx):
                            new_flags.append(curr_new_flag)
                        curr_new_flag += 1

                assert len(new_flags) == len(self.flag)
                assert len(np.bincount(new_flags)) == len(np.bincount(self.flag)) * self.sequences_split_num
                self.flag = np.array(new_flags, dtype=np.int64)

    def load_annotations(self, ann_file):
        """Load annotations from ann_file.

        Args:
            ann_file (str): Path of the annotation file.

        Returns:
            list[dict]: List of annotations sorted by timestamps.
        """
        data = mmcv.load(ann_file, file_format='pkl')
        # TODO sort_by_scene #
        if self.sort_by_scene:
            data_infos = list(sorted(data['infos'], key=lambda e: e['timestamp']))
        else:
            data_infos = data['infos']

        if self.mini_train == 'split':
            data_infos = data_infos[:len(data_infos)//4]
        elif self.mini_train:
            data_infos = data_infos[::self.mini_train]
        else:
            pass

        print(len(data_infos))
        # data_infos = data_infos[4000*4:]

    
        self.metadata = data['metadata']
        self.version = self.metadata['version']
        return data_infos
    
    def vectormap_pipeline(self, example, input_dict):
        '''
        `example` type: <class 'dict'>
            keys: 'img_metas', 'gt_bboxes_3d', 'gt_labels_3d', 'img';
                  all keys type is 'DataContainer';
                  'img_metas' cpu_only=True, type is dict, others are false;
                  'gt_labels_3d' shape torch.size([num_samples]), stack=False,
                                padding_value=0, cpu_only=False
                  'gt_bboxes_3d': stack=False, cpu_only=True
        '''
        # import ipdb;ipdb.set_trace()

        anns_results = self.vector_map.gen_vectorized_samples(input_dict['annotation'] if 'annotation' in input_dict.keys() else input_dict['ann_info'],
                     example=example, feat_down_sample=self.aux_seg['feat_down_sample'])
        
        '''
        anns_results, type: dict
            'gt_vecs_pts_loc': list[num_vecs], vec with num_points*2 coordinates
            'gt_vecs_pts_num': list[num_vecs], vec with num_points
            'gt_vecs_label': list[num_vecs], vec with cls index
        '''
        gt_vecs_label = to_tensor(anns_results['gt_vecs_label'])
        if isinstance(anns_results['gt_vecs_pts_loc'], LiDARInstanceLines):
            gt_vecs_pts_loc = anns_results['gt_vecs_pts_loc']
        else:
            gt_vecs_pts_loc = to_tensor(anns_results['gt_vecs_pts_loc'])
            try:
                gt_vecs_pts_loc = gt_vecs_pts_loc.flatten(1).to(dtype=torch.float32)
            except:
                # empty tensor, will be passed in train, 
                # but we preserve it for test
                gt_vecs_pts_loc = gt_vecs_pts_loc
        example['gt_labels_3d'] = DC(gt_vecs_label, cpu_only=False)
        example['gt_bboxes_3d'] = DC(gt_vecs_pts_loc, cpu_only=True)

        # gt_seg_mask = to_tensor(anns_results['gt_semantic_mask'])
        # gt_pv_seg_mask = to_tensor(anns_results['gt_pv_semantic_mask'])
        if anns_results['gt_semantic_mask'] is not None:
            example['gt_seg_mask'] = DC(to_tensor(anns_results['gt_semantic_mask']), cpu_only=False)
        if anns_results['gt_pv_semantic_mask'] is not None:
            example['gt_pv_seg_mask'] = DC(to_tensor(anns_results['gt_pv_semantic_mask']), cpu_only=False) 
        return example

    def prepare_train_data(self, index):
        """
        Training data preparation.
        Args:
            index (int): Index for accessing the target data.
        Returns:
            dict: Training data dict of the corresponding index.
        """
        data_queue = []

        # temporal aug
        prev_indexs_list = list(range(index-self.queue_length, index))
        random.shuffle(prev_indexs_list)
        prev_indexs_list = sorted(prev_indexs_list[1:], reverse=True)
        ##

        input_dict = self.get_data_info(index)
        if input_dict is None:
            return None
        frame_idx = input_dict['frame_idx']
        scene_token = input_dict['scene_token']
        self.pre_pipeline(input_dict)
        # import pdb;pdb.set_trace()
        example = self.pipeline(input_dict)
        example = self.vectormap_pipeline(example,input_dict)

        if self.dn_enabled:
            example["img_metas"].data.update(
                {"gt_bboxes_3d": example["gt_bboxes_3d"], "gt_labels_3d": example["gt_labels_3d"]})  # TODO dn

        if self.filter_empty_gt and \
                (example is None or ~(example['gt_labels_3d']._data != -1).any()):
            return None
        data_queue.insert(0, example)
        for i in prev_indexs_list:
            i = max(0, i)
            input_dict = self.get_data_info(i)
            if input_dict is None:
                return None
            if input_dict['frame_idx'] < frame_idx and input_dict['scene_token'] == scene_token:
                self.pre_pipeline(input_dict)
                example = self.pipeline(input_dict)
                example = self.vectormap_pipeline(example,input_dict)
                if self.filter_empty_gt and \
                        (example is None or ~(example['gt_labels_3d']._data != -1).any()):
                    return None
                frame_idx = input_dict['frame_idx']
            data_queue.insert(0, copy.deepcopy(example))
        return self.union2one(data_queue)

    def union2one(self, queue):
        """
        convert sample queue into one single sample.
        """
        # import ipdb;ipdb.set_trace()
        imgs_list = [each['img'].data for each in queue]
        metas_map = {}
        prev_pos = None
        prev_angle = None
        for i, each in enumerate(queue):
            metas_map[i] = each['img_metas'].data
            if i == 0:
                metas_map[i]['prev_bev'] = False
                prev_lidar2global = metas_map[i]['lidar2global']
                prev_pos = copy.deepcopy(metas_map[i]['can_bus'][:3])
                prev_angle = copy.deepcopy(metas_map[i]['can_bus'][-1])
                metas_map[i]['can_bus'][:3] = 0
                metas_map[i]['can_bus'][-1] = 0
                tmp_lidar2prev_lidar = np.eye(4)
                metas_map[i]['tmp_lidar2prev_lidar'] = tmp_lidar2prev_lidar
                tmp_lidar2prev_lidar_translation = tmp_lidar2prev_lidar[:3,3]
                tmp_lidar2prev_lidar_angle = quaternion_yaw(Quaternion(
                                                matrix=tmp_lidar2prev_lidar)) / np.pi * 180
                metas_map[i]['tmp_lidar2prev_lidar_translation'] = tmp_lidar2prev_lidar_translation
                metas_map[i]['tmp_lidar2prev_lidar_angle'] = tmp_lidar2prev_lidar_angle
            else:
                metas_map[i]['prev_bev'] = True
                tmp_lidar2global = metas_map[i]['lidar2global']
                tmp_lidar2prev_lidar = np.linalg.inv(prev_lidar2global)@tmp_lidar2global
                tmp_lidar2prev_lidar_translation = tmp_lidar2prev_lidar[:3,3]
                tmp_lidar2prev_lidar_angle = quaternion_yaw(Quaternion(
                                                matrix=tmp_lidar2prev_lidar)) / np.pi * 180
                tmp_pos = copy.deepcopy(metas_map[i]['can_bus'][:3])
                tmp_angle = copy.deepcopy(metas_map[i]['can_bus'][-1])
                metas_map[i]['can_bus'][:3] -= prev_pos
                metas_map[i]['can_bus'][-1] -= prev_angle
                metas_map[i]['tmp_lidar2prev_lidar'] = tmp_lidar2prev_lidar
                metas_map[i]['tmp_lidar2prev_lidar_translation'] = tmp_lidar2prev_lidar_translation
                metas_map[i]['tmp_lidar2prev_lidar_angle'] = tmp_lidar2prev_lidar_angle
                prev_pos = copy.deepcopy(tmp_pos)
                prev_angle = copy.deepcopy(tmp_angle)
                prev_lidar2global = copy.deepcopy(tmp_lidar2global)

        queue[-1]['img'] = DC(torch.stack(imgs_list),
                              cpu_only=False, stack=True)
        queue[-1]['img_metas'] = DC(metas_map, cpu_only=True)
        queue = queue[-1]
        return queue

    def get_prev_infos(self, index):  # TODO 추가

        curr_info = self.data_infos[index]
        lidar2ego = np.eye(4)
        lidar2ego[:3, :3] = Quaternion(curr_info['lidar2ego_rotation']).rotation_matrix
        lidar2ego[:3, 3] = curr_info['lidar2ego_translation']
        ego2global = np.eye(4)
        ego2global[:3, :3] = Quaternion(curr_info['ego2global_rotation']).rotation_matrix
        ego2global[:3, 3] = curr_info['ego2global_translation']
        lidar2global_curr = ego2global @ lidar2ego

        prev_infos = []
        prev_token = self.data_infos[index]['prev']
        while len(prev_infos) < 4:
            if prev_token == '':
                return prev_infos
            tmp = dict()
            for info in self.data_infos:
                if info['token'] == prev_token:
                    break
            tmp['token'] = prev_token
            lidar2ego = np.eye(4)
            lidar2ego[:3, :3] = Quaternion(info['lidar2ego_rotation']).rotation_matrix
            lidar2ego[:3, 3] = info['lidar2ego_translation']
            ego2global = np.eye(4)
            ego2global[:3, :3] = Quaternion(info['ego2global_rotation']).rotation_matrix
            ego2global[:3, 3] = info['ego2global_translation']

            lidar2global_prev = ego2global @ lidar2ego

            prev2curr = np.linalg.inv(lidar2global_curr) @ lidar2global_prev
            
            tmp['prev2curr'] = prev2curr
            gt_pts_list = []
            gt_label_list = []
            for k, v in info['annotation'].items():
                if k == 'centerline':
                    continue
                for instance in v:
                    line = LineString(instance)
                    distances = np.linspace(0, line.length, self.fixed_num)
                    sampled_points = np.array(
                        [list(line.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
                    gt_pts_list.append(sampled_points)
                    gt_label_list.append(self.MAPCLASSES.index(k))
                gt_pts_array = np.array(gt_pts_list)
                gt_pts_tensor = to_tensor(gt_pts_array)
                gt_pts_tensor = gt_pts_tensor.to(dtype=torch.float32)
                if len(gt_pts_tensor.shape) == 3:
                    gt_pts_tensor[:, :, 0] = torch.clamp(gt_pts_tensor[:, :, 0], min=-self.patch_size[1] / 2,
                                                         max=self.patch_size[1] / 2)
                    gt_pts_tensor[:, :, 1] = torch.clamp(gt_pts_tensor[:, :, 1], min=-self.patch_size[0] / 2,
                                                         max=self.patch_size[0] / 2)
                    gt_label_array = np.array(gt_label_list)
                    gt_label_tensor = to_tensor(gt_label_array)
                else:
                    gt_pts_tensor = torch.zeros([0, 20, 2])
                    gt_label_tensor = torch.zeros([0])
            tmp['gt_pts'] = gt_pts_tensor
            tmp['gt_label'] = gt_label_tensor

            self.canvas_size = [200, 100]
            self.scale_x = self.canvas_size[1] / self.patch_size[1]
            self.scale_y = self.canvas_size[0] / self.patch_size[0]

            gt_semantic_mask_0 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
            gt_semantic_mask_1 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
            gt_semantic_mask_2 = np.zeros((1, self.canvas_size[0], self.canvas_size[1]), dtype=np.uint8)
            
            for k, v in info['annotation'].items():
                if k == 'centerline':
                    continue
                for instance in v:
                    line = LineString(instance)
                    if self.MAPCLASSES.index(k) == 0:
                        self.line_ego_to_mask(line, gt_semantic_mask_0[0], color=1, thickness=3)
                    elif self.MAPCLASSES.index(k) == 1:
                        self.line_ego_to_mask(line, gt_semantic_mask_1[0], color=1, thickness=3)
                    elif self.MAPCLASSES.index(k) == 2:
                        self.line_ego_to_mask(line, gt_semantic_mask_2[0], color=1, thickness=3)
            
            gt_semantic_mask = np.concatenate(
                [gt_semantic_mask_0.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])
                    , gt_semantic_mask_1.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])
                    , gt_semantic_mask_2.reshape(1, 1, self.canvas_size[0], self.canvas_size[1])], 1)
            
            tmp['gt_seg'] = to_tensor(gt_semantic_mask)
            prev_infos.append(tmp)
            prev_token = info['prev']
        return prev_infos
    
    def line_ego_to_mask(self,
                         line_ego,
                         mask,
                         color=1,
                         thickness=3):
        ''' Rasterize a single line to mask.

        Args:
            line_ego (LineString): line
            mask (array): semantic mask to paint on
            color (int): positive label, default: 1
            thickness (int): thickness of rasterized lines, default: 3
        '''

        trans_x = self.canvas_size[1] / 2
        trans_y = self.canvas_size[0] / 2
        line_ego = affinity.scale(line_ego, self.scale_x, self.scale_y, origin=(0, 0))
        line_ego = affinity.affine_transform(line_ego, [1.0, 0.0, 0.0, 1.0, trans_x, trans_y])
        # print(np.array(list(line_ego.coords), dtype=np.int32).shape)
        coords = np.array(list(line_ego.coords), dtype=np.int32)[:, :2]
        coords = coords.reshape((-1, 2))
        assert len(coords) >= 2

        cv2.polylines(mask, np.int32([coords]), False, color=color, thickness=thickness)

    def get_data_info(self, index):
        """Get data info according to the given index.

        Args:
            index (int): Index of the sample data to get.

        Returns:
            dict: Data information that will be passed to the data \
                preprocessing pipelines. It includes the following keys:

                - sample_idx (str): Sample index.
                - pts_filename (str): Filename of point clouds.
                - sweeps (list[dict]): Infos of sweeps.
                - timestamp (float): Sample timestamp.
                - img_filename (str, optional): Image filename.
                - lidar2img (list[np.ndarray], optional): Transformations \
                    from lidar to different cameras.
                - ann_info (dict): Annotation info.
        """
        info = self.data_infos[index]
        # prev_infos = self.get_prev_infos(index)
        # standard protocal modified from SECOND.Pytorch
        input_dict = dict(
            sample_idx=info['token'],
            pts_filename=info['lidar_path'],
            lidar_path=info["lidar_path"],
            sweeps=info['sweeps'],
            ego2global_translation=info['ego2global_translation'],
            ego2global_rotation=info['ego2global_rotation'],
            lidar2ego_translation=info['lidar2ego_translation'],
            lidar2ego_rotation=info['lidar2ego_rotation'],
            prev_idx=info['prev'],
            next_idx=info['next'],
            scene_token=info['scene_token'],
            can_bus=info['can_bus'],
            frame_idx=info['frame_idx'],
            timestamp=info['timestamp'],
            map_location = info['map_location'],
            # prev_infos=prev_infos     # TODO dn
        )
        # lidar to ego transform
        lidar2ego = np.eye(4).astype(np.float32)
        lidar2ego[:3, :3] = Quaternion(info["lidar2ego_rotation"]).rotation_matrix
        lidar2ego[:3, 3] = info["lidar2ego_translation"]
        input_dict["lidar2ego"] = lidar2ego
        if self.modality['use_camera']:
            image_paths = []
            lidar2img_rts = []
            lidar2cam_rts = []
            cam_intrinsics = []
            input_dict["camera2ego"] = []
            input_dict["camera_intrinsics"] = []
            input_dict["camego2global"] = []
            for cam_type, cam_info in info['cams'].items():
                image_paths.append(cam_info['data_path'])
                # obtain lidar to image transformation matrix
                lidar2cam_r = np.linalg.inv(cam_info['sensor2lidar_rotation'])
                lidar2cam_t = cam_info[
                    'sensor2lidar_translation'] @ lidar2cam_r.T
                lidar2cam_rt = np.eye(4)
                lidar2cam_rt[:3, :3] = lidar2cam_r.T
                lidar2cam_rt[3, :3] = -lidar2cam_t
                lidar2cam_rt_t = lidar2cam_rt.T

                if self.noise == 'rotation':
                    lidar2cam_rt_t = add_rotation_noise(lidar2cam_rt_t, std=self.noise_std)
                elif self.noise == 'translation':
                    lidar2cam_rt_t = add_translation_noise(
                        lidar2cam_rt_t, std=self.noise_std)

                intrinsic = cam_info['cam_intrinsic']
                viewpad = np.eye(4)
                viewpad[:intrinsic.shape[0], :intrinsic.shape[1]] = intrinsic
                lidar2img_rt = (viewpad @ lidar2cam_rt_t)
                lidar2img_rts.append(lidar2img_rt)

                cam_intrinsics.append(viewpad)
                lidar2cam_rts.append(lidar2cam_rt_t)

                # camera to ego transform
                camera2ego = np.eye(4).astype(np.float32)
                camera2ego[:3, :3] = Quaternion(
                    cam_info["sensor2ego_rotation"]
                ).rotation_matrix
                camera2ego[:3, 3] = cam_info["sensor2ego_translation"]
                input_dict["camera2ego"].append(camera2ego)

                # camego to global transform
                camego2global = np.eye(4, dtype=np.float32)
                camego2global[:3, :3] = Quaternion(
                    cam_info['ego2global_rotation']).rotation_matrix
                camego2global[:3, 3] = cam_info['ego2global_translation']
                camego2global = torch.from_numpy(camego2global)
                input_dict["camego2global"].append(camego2global)

                # camera intrinsics
                camera_intrinsics = np.eye(4).astype(np.float32)
                camera_intrinsics[:3, :3] = cam_info["cam_intrinsic"]
                input_dict["camera_intrinsics"].append(camera_intrinsics)

            input_dict.update(
                dict(
                    img_filename=image_paths,
                    lidar2img=lidar2img_rts,
                    cam_intrinsic=cam_intrinsics,
                    lidar2cam=lidar2cam_rts,
                ))

        # if not self.test_mode:
        #     # annos = self.get_ann_info(index)
        input_dict['ann_info'] = info['annotation']

        rotation = Quaternion(input_dict['ego2global_rotation'])
        translation = input_dict['ego2global_translation']
        can_bus = input_dict['can_bus']
        can_bus[:3] = translation
        can_bus[3:7] = rotation
        patch_angle = quaternion_yaw(rotation) / np.pi * 180
        if patch_angle < 0:
            patch_angle += 360
        can_bus[-2] = patch_angle / 180 * np.pi
        can_bus[-1] = patch_angle


        lidar2ego = np.eye(4)
        lidar2ego[:3,:3] = Quaternion(input_dict['lidar2ego_rotation']).rotation_matrix
        lidar2ego[:3, 3] = input_dict['lidar2ego_translation']
        ego2global = np.eye(4)
        ego2global[:3,:3] = Quaternion(input_dict['ego2global_rotation']).rotation_matrix
        ego2global[:3, 3] = input_dict['ego2global_translation']
        lidar2global = ego2global @ lidar2ego
        input_dict['lidar2global'] = lidar2global
        return input_dict

    def prepare_test_data(self, index):
        """Prepare data for testing.

        Args:
            index (int): Index for accessing the target data.

        Returns:
            dict: Testing data dict of the corresponding index.
        """
        input_dict = self.get_data_info(index)
        self.pre_pipeline(input_dict)
        example = self.pipeline(input_dict)
        if self.is_vis_on_test:
            example = self.vectormap_pipeline(example, input_dict)
        return example

    def __getitem__(self, idx):
        """Get item from infos according to the given index.
        Returns:
            dict: Data dictionary of the corresponding index.
        """
        if self.test_mode:
            return self.prepare_test_data(idx)
        while True:

            data = self.prepare_train_data(idx)
            if data is None:
                idx = self._rand_another(idx)
                continue
            return data
    def _format_gt(self):
        gt_annos = []
        print('Start to convert gt map format...')
        assert self.map_ann_file is not None
        if (not os.path.exists(self.map_ann_file)) :
            dataset_length = len(self)
            prog_bar = mmcv.ProgressBar(dataset_length)
            mapped_class_names = self.MAPCLASSES
            for sample_id in range(dataset_length):
                sample_token = self.data_infos[sample_id]['token']
                gt_anno = {}
                gt_anno['sample_token'] = sample_token
                # gt_sample_annos = []
                gt_sample_dict = {}
                gt_sample_dict = self.vectormap_pipeline(gt_sample_dict, self.data_infos[sample_id])
                gt_labels = gt_sample_dict['gt_labels_3d'].data.numpy()
                gt_vecs = gt_sample_dict['gt_bboxes_3d'].data.instance_list
                gt_vec_list = []
                for i, (gt_label, gt_vec) in enumerate(zip(gt_labels, gt_vecs)):
                    name = mapped_class_names[gt_label]
                    anno = dict(
                        pts=np.array(list(gt_vec.coords)),
                        pts_num=len(list(gt_vec.coords)),
                        cls_name=name,
                        type=gt_label,
                    )
                    gt_vec_list.append(anno)
                gt_anno['vectors']=gt_vec_list
                gt_annos.append(gt_anno)

                prog_bar.update()
            nusc_submissions = {
                'GTs': gt_annos
            }
            print('\n GT anns writes to', self.map_ann_file)
            mmcv.dump(nusc_submissions, self.map_ann_file)
        else:
            print(f'{self.map_ann_file} exist, not update')

    def _format_bbox(self, results, jsonfile_prefix=None):
        """Convert the results to the standard format.

        Args:
            results (list[dict]): Testing results of the dataset.
            jsonfile_prefix (str): The prefix of the output jsonfile.
                You can specify the output directory/filename by
                modifying the jsonfile_prefix. Default: None.

        Returns:
            str: Path of the output json file.
        """
        assert self.map_ann_file is not None
        pred_annos = []
        mapped_class_names = self.MAPCLASSES
        # import pdb;pdb.set_trace()
        print('Start to convert map detection format...')
        for sample_id, det in enumerate(mmcv.track_iter_progress(results)):
            pred_anno = {}
            vecs = output_to_vecs(det, self.pts_thr)
            try:
                ref_logits = det['ref_logits'].detach().cpu().numpy()  # TODO 변경
                pred_anno['ref_logits'] = ref_logits  # TODO 변경
            except:
                pass
            sample_token = self.data_infos[sample_id]['token']
            pred_anno['sample_token'] = sample_token
            pred_vec_list=[]
            for i, vec in enumerate(vecs):
                name = mapped_class_names[vec['label']]
                anno = dict(
                    pts=vec['pts'],
                    pts_num=len(vec['pts']),
                    cls_name=name,
                    type=vec['label'],
                    confidence_level=vec['score'])
                pred_vec_list.append(anno)

            pred_anno['vectors'] = pred_vec_list
            pred_annos.append(pred_anno)

        if not os.path.exists(self.map_ann_file):
            self._format_gt()
        else:
            print(f'{self.map_ann_file} exist, not update')

        nusc_submissions = {
            'meta': self.modality,
            'results': pred_annos,

        }

        mmcv.mkdir_or_exist(jsonfile_prefix)
        res_path = osp.join(jsonfile_prefix, 'nuscmap_results.json')
        print('Results writes to', res_path)
        mmcv.dump(nusc_submissions, res_path)
        return res_path

    def to_gt_vectors(self,
                      gt_dict):
        # import pdb;pdb.set_trace()
        gt_labels = gt_dict['gt_labels_3d'].data
        gt_instances = gt_dict['gt_bboxes_3d'].data.instance_list

        gt_vectors = []

        for gt_instance, gt_label in zip(gt_instances, gt_labels):
            pts, pts_num = sample_pts_from_line(gt_instance, patch_size=self.patch_size)
            gt_vectors.append({
                'pts': pts,
                'pts_num': pts_num,
                'type': int(gt_label)
            })
        vector_num_list = {}
        for i in range(self.NUM_MAPCLASSES):
            vector_num_list[i] = []
        for vec in gt_vectors:
            if vector['pts_num'] >= 2:
                vector_num_list[vector['type']].append((LineString(vector['pts'][:vector['pts_num']]), vector.get('confidence_level', 1)))
        return gt_vectors

    def _evaluate_single(self,
                         result_path,
                         logger=None,
                         metric='chamfer',
                         result_name='pts_bbox'):
        """Evaluation for a single model in nuScenes protocol.

        Args:
            result_path (str): Path of the result file.
            logger (logging.Logger | str | None): Logger used for printing
                related information during evaluation. Default: None.
            metric (str): Metric name used for evaluation. Default: 'bbox'.
            result_name (str): Result name in the metric prefix.
                Default: 'pts_bbox'.

        Returns:
            dict: Dictionary of evaluation details.
        """
        from projects.mmdet3d_plugin.datasets.map_utils.mean_ap import eval_map
        from projects.mmdet3d_plugin.datasets.map_utils.mean_ap import format_res_gt_by_classes
        result_path = osp.abspath(result_path)
        detail = dict()
        
        print('Formating results & gts by classes')
        with open(result_path,'r') as f:
            pred_results = json.load(f)
        gen_results = pred_results['results']
        with open(self.map_ann_file,'r') as ann_f:
            gt_anns = json.load(ann_f)
        annotations = gt_anns['GTs']
        cls_gens, cls_gts = format_res_gt_by_classes(result_path,
                                                     gen_results,
                                                     annotations,
                                                     cls_names=self.MAPCLASSES,
                                                     num_pred_pts_per_instance=self.fixed_num,
                                                     eval_use_same_gt_sample_num_flag=self.eval_use_same_gt_sample_num_flag,
                                                     pc_range=self.pc_range)

        metrics = metric if isinstance(metric, list) else [metric]
        allowed_metrics = ['chamfer', 'iou']
        for metric in metrics:
            if metric not in allowed_metrics:
                raise KeyError(f'metric {metric} is not supported')

        for metric in metrics:
            print('-*'*10+f'use metric:{metric}'+'-*'*10)

            if metric == 'chamfer':
                thresholds = [0.5,1.0,1.5]
            elif metric == 'iou':
                thresholds= np.linspace(.5, 0.95, int(np.round((0.95 - .5) / .05)) + 1, endpoint=True)
            cls_aps = np.zeros((len(thresholds),self.NUM_MAPCLASSES))

            for i, thr in enumerate(thresholds):
                print('-*'*10+f'threshhold:{thr}'+'-*'*10)
                mAP, cls_ap = eval_map(
                                gen_results,
                                annotations,
                                cls_gens,
                                cls_gts,
                                threshold=thr,
                                cls_names=self.MAPCLASSES,
                                logger=logger,
                                num_pred_pts_per_instance=self.fixed_num,
                                pc_range=self.pc_range,
                                metric=metric)
                for j in range(self.NUM_MAPCLASSES):
                    cls_aps[i, j] = cls_ap[j]['ap']

            for i, name in enumerate(self.MAPCLASSES):
                print('{}: {}'.format(name, cls_aps.mean(0)[i]))
                detail['NuscMap_{}/{}_AP'.format(metric,name)] =  cls_aps.mean(0)[i]
            print('map: {}'.format(cls_aps.mean(0).mean()))
            detail['NuscMap_{}/mAP'.format(metric)] = cls_aps.mean(0).mean()

            for i, name in enumerate(self.MAPCLASSES):
                for j, thr in enumerate(thresholds):
                    if metric == 'chamfer':
                        detail['NuscMap_{}/{}_AP_thr_{}'.format(metric,name,thr)]=cls_aps[j][i]
                    elif metric == 'iou':
                        if thr == 0.5 or thr == 0.75:
                            detail['NuscMap_{}/{}_AP_thr_{}'.format(metric,name,thr)]=cls_aps[j][i]

        return detail


    def evaluate(self,
                 results,
                 metric='bbox',
                 logger=None,
                 jsonfile_prefix=None,
                 result_names=['pts_bbox'],
                 show=False,
                 out_dir=None,
                 pipeline=None):
        """Evaluation in nuScenes protocol.

        Args:
            results (list[dict]): Testing results of the dataset.
            metric (str | list[str]): Metrics to be evaluated.
            logger (logging.Logger | str | None): Logger used for printing
                related information during evaluation. Default: None.
            jsonfile_prefix (str | None): The prefix of json files. It includes
                the file path and the prefix of filename, e.g., "a/b/prefix".
                If not specified, a temp file will be created. Default: None.
            show (bool): Whether to visualize.
                Default: False.
            out_dir (str): Path to save the visualization results.
                Default: None.
            pipeline (list[dict], optional): raw data loading for showing.
                Default: None.

        Returns:
            dict[str, float]: Results of each evaluation metric.
        """
        # jsonfile_prefix =jsonfile_prefix.split('/')[:2]
        # path = jsonfile_prefix[0] + '/' + jsonfile_prefix[1]
        result_files, tmp_dir = self.format_results(results, jsonfile_prefix)
        if isinstance(result_files, dict):
            results_dict = dict()
            for name in result_names:
                print('Evaluating bboxes of {}'.format(name))
                ret_dict = self._evaluate_single(result_files[name], metric=metric)
            results_dict.update(ret_dict)
        elif isinstance(result_files, str):
            results_dict = self._evaluate_single(result_files, metric=metric)

        if tmp_dir is not None:
            tmp_dir.cleanup()

        if show:
            self.show(results, out_dir, pipeline=pipeline)
        return results_dict

    def Vector_evaluate(self,
                        pred_vectors,
                        scores,
                        groundtruth,
                        thresholds,
                        metric):
        from projects.mmdet3d_plugin.datasets.map_utils.AP import instance_match
        ''' Do single-frame matching for one class.

        Args:
            pred_vectors (List): List[vector(ndarray) (different length)],
            scores (List): List[score(float)]
            groundtruth (List): List of vectors
            thresholds (List): List of thresholds

        Returns:
            tp_fp_score_by_thr (Dict): matching results at different thresholds
                e.g. {0.5: (M, 2), 1.0: (M, 2), 1.5: (M, 2)}
        '''
        SAMPLE_DIST = 0.3
        pred_lines = []

        # interpolate predictions
        for vector in pred_vectors:
            vector = np.array(vector)
            # vector_interp = self.interp_fixed_num(vector, INTERP_NUM)
            vector_interp = self.interp_fixed_dist(vector, SAMPLE_DIST)
            pred_lines.append(vector_interp)

        # interpolate groundtruth
        gt_lines = []
        for vector in groundtruth:
            # vector_interp = self.interp_fixed_num(vector, INTERP_NUM)
            vector_interp = self.interp_fixed_dist(vector, SAMPLE_DIST)
            gt_lines.append(vector_interp)

        scores = np.array(scores)
        tp_fp_list = instance_match(pred_lines, scores, gt_lines, thresholds, metric[0])  # (M, 2)
        tp_fp_score_by_thr = {}
        for i, thr in enumerate(thresholds):
            tp, fp = tp_fp_list[i]
            tp_fp_score = np.hstack([tp[:, None], fp[:, None], scores[:, None]])
            tp_fp_score_by_thr[thr] = tp_fp_score

        return tp_fp_score_by_thr  # {0.5: (M, 2), 1.0: (M, 2), 1.5: (M, 2)}

    def interp_fixed_dist(self,
                          vector,
                          sample_dist):
        ''' Interpolate a line at fixed interval.

        Args:
            vector (LineString): vector
            sample_dist (float): sample interval

        Returns:
            points (array): interpolated points, shape (N, 2)
        '''

        line = LineString(vector)
        distances = list(np.arange(sample_dist, line.length, sample_dist))
        # make sure to sample at least two points when sample_dist > line.length
        distances = [0, ] + distances + [line.length, ]

        sampled_points = np.array([list(line.interpolate(distance).coords)
                                   for distance in distances]).squeeze()

        return sampled_points

    def _evaluate_single_dist(self,
                         result_path,
                         logger=None,
                         metric='chamfer',
                         result_name='pts_bbox'):

        from projects.mmdet3d_plugin.datasets.map_utils.AP import average_precision
        result_path = osp.abspath(result_path)
        detail = dict()

        print('Formating results & gts by classes')
        with open(result_path, 'r') as f:
            pred_results = json.load(f)
        gen_results = pred_results['results']
        with open(self.map_ann_file, 'r') as ann_f:
            gt_anns = json.load(ann_f)
        annotations = gt_anns['GTs']
        
        results_tokes = [gen_resu['sample_token']  for gen_resu in gen_results]
        
        CAT2ID = {'ped_crossing': 0, 'divider': 1, 'boundary': 2}
        id2cat = {v: k for k, v in CAT2ID.items()}
        samples_by_cls = {label: [] for label in id2cat.keys()}
        num_gts = {label: 0 for label in id2cat.keys()}
        num_preds = {label: 0 for label in id2cat.keys()}
        
        # align by token
        for ge_idx in range(len(annotations)):
            if annotations[ge_idx]['sample_token'] in results_tokes:
                if annotations[ge_idx]['sample_token'] == gen_results[ge_idx]['sample_token']:
                    pred = gen_results[ge_idx]['vectors']
                else:
                    for temp_idx in range(len(gen_results)):
                        if annotations[ge_idx]['sample_token'] == gen_results[temp_idx]['sample_token']:
                            pred = gen_results[temp_idx]['vectors']

            # for every sample
            vectors_by_cls = {label: [] for label in id2cat.keys()}
            scores_by_cls = {label: [] for label in id2cat.keys()}

            gt = annotations[ge_idx]['vectors']
            # for every sample
            GT_vectors_by_cls = {label: [] for label in id2cat.keys()}
            
            for i in range(len(gt)):
                # i-th pred line in sample
                label = gt[i]['type']
                vector = gt[i]['pts']

                GT_vectors_by_cls[label].append(vector)
            c = 0
            for i in range(len(pred)):
                # i-th pred line in sample
                label = pred[i]['type']
                vector = pred[i]['pts']
                score = pred[i]['confidence_level']
                
                if len(vector) < 2:
                    c+=1
                    continue

                vectors_by_cls[label].append(vector)
                scores_by_cls[label].append(score)

            for label, cat in id2cat.items():
                new_sample = (vectors_by_cls[label], scores_by_cls[label], GT_vectors_by_cls[label])
                num_gts[label] += len(GT_vectors_by_cls[label])
                num_preds[label] += len(scores_by_cls[label])
                samples_by_cls[label].append(new_sample)

        result_dict = {}

        print(f'\nevaluating {len(id2cat)} categories...')
        THRESHOLDS = [0.5, 1.0, 1.5]
        sum_mAP = 0
        pbar = mmcv.ProgressBar(len(id2cat))
        for label in id2cat.keys():
            samples = samples_by_cls[label]  # List[(pred_lines, scores, gts)]
            result_dict[id2cat[label]] = {
                'num_gts': num_gts[label],
                'num_preds': num_preds[label]
            }
            sum_AP = 0

            fn = partial(self.Vector_evaluate, thresholds=THRESHOLDS, metric=metric)
            # if self.n_workers > 0:
            #     tpfp_score_list = pool.starmap(fn, samples)
            # else:
            tpfp_score_list = []

            for sample in samples:
                tpfp_score_list.append(fn(*sample))

            for thr in THRESHOLDS:
                tp_fp_score = [i[thr] for i in tpfp_score_list]
                tp_fp_score = np.vstack(tp_fp_score)  # (num_dets, 3)
                sort_inds = np.argsort(-tp_fp_score[:, -1])

                tp = tp_fp_score[sort_inds, 0]  # (num_dets,)
                fp = tp_fp_score[sort_inds, 1]  # (num_dets,)
                tp = np.cumsum(tp, axis=0)
                fp = np.cumsum(fp, axis=0)
                eps = np.finfo(np.float32).eps
                recalls = tp / np.maximum(num_gts[label], eps)
                precisions = tp / np.maximum((tp + fp), eps)

                AP = average_precision(recalls, precisions, 'area')
                sum_AP += AP
                result_dict[id2cat[label]].update({f'AP@{thr}': AP})

            pbar.update()

            AP = sum_AP / len(THRESHOLDS)
            sum_mAP += AP

            result_dict[id2cat[label]].update({f'AP': AP})

        mAP = sum_mAP / len(id2cat.keys())
        result_dict.update({'mAP': mAP})

        # print results
        table = prettytable.PrettyTable(['category', 'num_preds', 'num_gts'] +
                                        [f'AP@{thr}' for thr in THRESHOLDS] + ['AP'])
        for label in id2cat.keys():
            table.add_row([
                id2cat[label],
                result_dict[id2cat[label]]['num_preds'],
                result_dict[id2cat[label]]['num_gts'],
                *[round(result_dict[id2cat[label]][f'AP@{thr}'], 4) for thr in THRESHOLDS],
                round(result_dict[id2cat[label]]['AP'], 4),
            ])

        from mmcv.utils import print_log
        print_log('\n' + str(table), logger=logger)
        print_log(f'mAP = {mAP:.4f}\n', logger=logger)

        new_result_dict = {}
        for name in CAT2ID:
            new_result_dict[name] = result_dict[name]['AP']

        return annotations

    def evaluate_dist(self,
                      results,
                      metric='bbox',
                      logger=None,
                      jsonfile_prefix=None,
                      result_names=['pts_bbox'],
                      show=False,
                      out_dir=None,
                      pipeline=None):
        result_files, tmp_dir = self.format_results(results, jsonfile_prefix)
        
        if isinstance(result_files, dict):
            results_dict = dict()
            for name in result_names:
                print('Evaluating bboxes of {}'.format(name))
                ret_dict = self._evaluate_single_dist(result_files[name], metric=metric)
            results_dict.update(ret_dict)
        elif isinstance(result_files, str):
            results_dict = self._evaluate_single_dist(result_files, metric=metric)

        if tmp_dir is not None:
            tmp_dir.cleanup()

        if show:
            self.show(results, out_dir, pipeline=pipeline)
        return results_dict

def output_to_vecs(detection, pts_thr ):
    box3d = detection['boxes_3d'].numpy()
    scores = detection['scores_3d'].numpy()
    labels = detection['labels_3d'].numpy()
    pts = detection['pts_3d'].numpy()

    # else:
    vec_list = []
    for i in range(box3d.shape[0]):
        # if pts.shape[1] > 41:
        #     if calculate_polyline_length(pts[i]) < 40:
        #         pts_sam = pts[i][::(pts[i].shape[0]//20)]
        #     elif calculate_polyline_length(pts[i]) < 100:
        #         pts_sam = pts[i][::(pts[i].shape[0] // 40)]
        #     else:
        #         pts_sam = pts[i]
        #     vec = dict(
        #         bbox=box3d[i],  # xyxy
        #         label=labels[i],
        #         score=scores[i],
        #         pts=pts_sam,
        #     )
        #     vec_list.append(vec)
        # else:
        #     vec = dict(
        #         bbox = box3d[i], # xyxy
        #         label=labels[i],
        #         score=scores[i],
        #         pts=pts[i],
        #     )
        #     vec_list.append(vec)

        vec = dict(
            bbox=box3d[i],  # xyxy
            label=labels[i],
            score=scores[i],
            pts=pts[i],
        )
        vec_list.append(vec)
    return vec_list

def calculate_polyline_length(points):
    length = 0
    for i in range(len(points) - 1):
        length += np.linalg.norm(points[i + 1] - points[i])
    return length


def sample_pts_from_line(line, 
                         fixed_num=-1,
                         sample_dist=1,
                         normalize=False,
                         patch_size=None,
                         padding=False,
                         num_samples=250,):
    if fixed_num < 0:
        distances = np.arange(0, line.length, sample_dist)
        sampled_points = np.array([list(line.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)
    else:
        # fixed number of points, so distance is line.length / fixed_num
        distances = np.linspace(0, line.length, fixed_num)
        sampled_points = np.array([list(line.interpolate(distance).coords) for distance in distances]).reshape(-1, 2)

    if normalize:
        sampled_points = sampled_points / np.array([patch_size[1], patch_size[0]])

    num_valid = len(sampled_points)

    if not padding or fixed_num > 0:
        # fixed num sample can return now!
        return sampled_points, num_valid

    # fixed distance sampling need padding!
    num_valid = len(sampled_points)

    if fixed_num < 0:
        if num_valid < num_samples:
            padding = np.zeros((num_samples - len(sampled_points), 2))
            sampled_points = np.concatenate([sampled_points, padding], axis=0)
        else:
            sampled_points = sampled_points[:num_samples, :]
            num_valid = num_samples

        if normalize:
            sampled_points = sampled_points / np.array([patch_size[1], patch_size[0]])
            num_valid = len(sampled_points)

    return sampled_points, num_valid

