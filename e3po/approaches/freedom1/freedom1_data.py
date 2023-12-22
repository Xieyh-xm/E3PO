# E3PO, an open platform for 360˚ video streaming simulation and evaluation.
# Copyright 2023 ByteDance Ltd. and/or its affiliates
#
# This file is part of E3PO.
#
# E3PO is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# E3PO is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see:
#    <https://www.gnu.org/licenses/old-licenses/gpl-2.0.en.html>

import os.path as osp
import os
import cv2
from tqdm import tqdm
from copy import deepcopy
import numpy as np
import json

from e3po.utils.registry import data_registry
from e3po.utils import pre_processing_client_log, write_json
from e3po.data.transcoding_data import TranscodingData

@data_registry.register()
class Freedom1Data(TranscodingData):
    """
    Freedom1 data. This is implemented by inheriting the parent class
    TranscodingData.

    Parameters
    ----------
    opt : dict
        Configurations.

    Notes
    -----
    Almost all class public attributes are directly read or indirectly processed from the yaml configuration file.
    Their specific meanings can be found in 'docs/Config.md'.
    """
    def __init__(self, opt):
        super(Freedom1Data, self).__init__(opt)
        freedom1 = opt['method_settings']
        self.decision_delay = freedom1['decision_delay']
        self.pre_download_duration = freedom1['pre_download_duration']
        self.crop_factor = [eval(v) for v in freedom1['crop_factor']]
        self.scale_factors = {k: eval(v) for k, v in freedom1['scale_factors'].items()}
        self.vam_size = freedom1['vam_size']
        self.decision_location = freedom1['decision_location'].lower()
        assert self.decision_location in ['client', 'server'], "[error] decision_location wrong. It should be set to the value in ['client', 'server']"
        self.rtt = opt['network_trace']['rtt'] * 0.5 if self.decision_location == 'server' else 0
        json_path = osp.join(self.work_folder, 'video_size.json')
        self.video_size = None
        if os.path.exists(json_path):
            with open(json_path, encoding='UTF-8') as f:
                self.video_size = json.load(f)
        self.chunk_idx = 0

    def process_video(self):
        self._convert_ori_video()  # 将源视频转换投影方式
        self._generate_viewport()  # 进行视点预测，根据预测结果遍历所有qp生成不同qp的视窗内容（以帧为单位存为jpeg视频）
        self._generate_h264()      # 将VAM帧转换为mp4视频并基于此视频提取H264帧
        self._get_viewport_size()  # 获取每一帧对应的视窗的size，记录在video_size.json中；进行质量选择，记录在decision.json中
        self._del_intermediate_file(self.work_folder, ['converted'], ['.json'])

    def _convert_ori_video(self):  # 将源视频转换投影方式
        """Convert original video's projection format and qp value."""
        os.makedirs(self.work_folder, exist_ok=True)
        os.chdir(self.work_folder)

        self.logger.info(f'[converting origin video] start; {self.ori_projection_mode} to {self.projection_mode}')
        cmd = f"{self.ffmpeg} " \
              f"-i {self.ori_video_path} " \
              f"-threads {self.ffmpeg_thread} " \
              f"-c:v libx264 " \
              f"-ss 0:0:0 " \
              f"-to {self.video_duration_str} " \
              f"-preset faster " \
              f"-vf v360={self.ori_ffmpeg_vf_option}:{self.target_ffmpeg_vf_option}" \
              f",scale={self.video_width}x{self.video_height} " \
              f"-y {self.projection_mode}.mp4 " \
              f"-loglevel {self.ffmpeg_loglevel}"
        self.logger.debug(cmd)
        os.system(cmd)
        self.logger.info('[converting origin video] end')

    def _generate_viewport(self):  # 进行视点预测，根据预测结果遍历所有qp生成不同qp的视窗内容（以帧为单位存为jpeg视频）
        """Generate VAM frame."""
        client_record = pre_processing_client_log(deepcopy(self.opt))
        client_ts_list = list(client_record.keys())
        base_ts = client_ts_list[0]

        self.logger.info("[generate vam] start")
        input_video_path = osp.join(self.work_folder, f"{self.projection_mode}.mp4")
        quality_bar = tqdm(self.quality_list, leave=False)
        for quality in quality_bar:  # 遍历quality等级
            quality_bar.set_description(f"[generate vam] qp={quality}")

            output_vam_path = osp.join(self.work_folder, f"qp{quality}", 'vams')
            os.makedirs(output_vam_path, exist_ok=True)

            video = cv2.VideoCapture()
            assert video.open(input_video_path), f"can't read video[{input_video_path}]"  # 获取进行投影变换后的视频
            frame_num = int(video.get(cv2.CAP_PROP_FRAME_COUNT))

            frame_bar = tqdm(range(frame_num), leave=False)
            for frame_idx in frame_bar:
                frame_bar.set_description(f"[generating vam] vam_idx={frame_idx}")

                ret, frame = video.read()
                if not ret:
                    break

                frame_ts = base_ts + frame_idx * 1000.0 / self.video_fps  # 该视频帧的时间戳
                motion_idx = 0
                for i, ts in enumerate(client_ts_list):  # ts：客户端的当前时间
                    if ts + self.rtt <= frame_ts - self.decision_delay:  # ts + self.rtt：从客户端传输到服务器端后的当前时间
                        motion_idx = i
                    else:
                        break

                scale = client_record[client_ts_list[motion_idx]]['scale']  # 获取最新的用户的头部运动轨迹的信息
                yaw = client_record[client_ts_list[motion_idx]]['yaw']
                pitch = client_record[client_ts_list[motion_idx]]['pitch']

                src_height, src_width = frame.shape[:2]  # 获取转换投影方式后的视频的宽高
                start_width = src_width * (self.scale_factors[scale] - self.crop_factor[0]) / 2
                start_height = src_height * (self.scale_factors[scale] - self.crop_factor[1]) / 2

                # u: azimuthal angle, v: polar angle
                u = (np.linspace(0.5, self.vam_size[0] - 0.5, self.vam_size[0]) + start_width) / (
                        src_width * self.scale_factors[scale]) * np.pi * 2
                v = (np.linspace(0.5, self.vam_size[1] - 0.5, self.vam_size[1]) + start_height) / (
                        src_height * self.scale_factors[scale]) * np.pi

                # convert the image coordinates to coordinates in 3D
                x = np.outer(np.sin(v), np.cos(u))
                y = np.outer(np.sin(v), np.sin(u))
                z = np.outer(np.cos(v), np.ones(np.size(u)))

                # rotation angles  旋转角度
                a, b, r = [yaw, pitch, 0]

                # rotation matrix  旋转矩阵
                rot_a = np.array([np.cos(a) * np.cos(b), np.cos(a) * np.sin(b) * np.sin(r) - np.sin(a) * np.cos(r),
                                  np.cos(a) * np.sin(b) * np.cos(r) + np.sin(a) * np.sin(r)])
                rot_b = np.array([np.sin(a) * np.cos(b), np.sin(a) * np.sin(b) * np.sin(r) + np.cos(a) * np.cos(r),
                                  np.sin(a) * np.sin(b) * np.cos(r) - np.cos(a) * np.sin(r)])
                rot_c = np.array([-np.sin(b), np.cos(b) * np.sin(r), np.cos(b) * np.cos(r)])

                # rotate the image to the correct place
                xx = rot_a[0] * x + rot_a[1] * y + rot_a[2] * z
                yy = rot_b[0] * x + rot_b[1] * y + rot_b[2] * z
                zz = rot_c[0] * x + rot_c[1] * y + rot_c[2] * z
                xx = np.clip(xx, -1, 1)
                yy = np.clip(yy, -1, 1)
                zz = np.clip(zz, -1, 1)

                # calculate the (u, v) in the original equirectangular map
                map_u = ((np.arctan2(yy, xx) + 2 * np.pi) % (2 * np.pi)) * src_width / (2 * np.pi) - 0.5
                map_v = np.arccos(zz) * src_height / np.pi - 0.5
                map_u = np.clip(map_u, 0, src_width - 1)
                map_v = np.clip(map_v, 0, src_height - 1)
                dstMap_u, dstMap_v = cv2.convertMaps(map_u.astype(np.float32), map_v.astype(np.float32), cv2.CV_16SC2)

                # remap the frame according to pitch and yaw
                vam_frame = cv2.remap(frame, dstMap_u, dstMap_v, cv2.INTER_LINEAR)
                cv2.imwrite(osp.join(output_vam_path, f"{frame_idx}.jpeg"), vam_frame, [cv2.IMWRITE_JPEG_QUALITY, 100])

            frame_bar.close()
        quality_bar.close()
        self.logger.info("[generate vam] end")

    def _generate_h264(self):  # 将VAM帧转换为mp4视频并基于此视频提取H264帧
        """Convert VAM frames into videos and extract H264 frames from them."""
        self.logger.info("[generate h264] start")

        quality_bar = tqdm(self.quality_list, leave=False)
        for quality in quality_bar:
            quality_bar.set_description(f"[generate h264] qp={quality}")

            input_vam_path = osp.join(self.work_folder, f"qp{quality}", 'vams')
            os.chdir(input_vam_path)
            output_h264_path = osp.join(self.work_folder, f"qp{quality}", 'h264')
            os.makedirs(output_h264_path, exist_ok=True)

            cmd = f"{self.ffmpeg} " \
                  f"-r {self.video_fps} " \
                  f"-start_number 0 " \
                  f"-i %d.jpeg " \
                  f"-threads {self.ffmpeg_thread} " \
                  f"-preset faster " \
                  f"-c:v libx264 " \
                  f"-g 150 " \
                  f"-bf 0 " \
                  f"-qp {quality} " \
                  f"-y {osp.join(self.work_folder, f'converted_{quality}.mp4')} " \
                  f"-loglevel {self.ffmpeg_loglevel}"
            self.logger.debug(cmd)
            os.system(cmd)

            cmd = f"{self.ffmpeg} " \
                  f"-i {osp.join(self.work_folder, f'converted_{quality}.mp4')} " \
                  f"-threads {self.ffmpeg_thread} " \
                  f"-f image2 " \
                  f"-vcodec copy " \
                  f"-bsf h264_mp4toannexb " \
                  f"-y {osp.join(output_h264_path, '%d.h264')} " \
                  f"-loglevel {self.ffmpeg_loglevel}"
            self.logger.debug(cmd)
            os.system(cmd)
        quality_bar.close()
        self.logger.info("[generate h264] end")

    def _get_viewport_size(self):  # 获取每一帧对应的视窗的size，记录在video_size.json中；进行质量选择，记录在decision.json中
        """Read the processed video file size and write it to video_size.json"""
        self.logger.info('[get vam size]')

        client_record = pre_processing_client_log(deepcopy(self.opt))
        client_ts_list = list(client_record.keys())
        base_ts = client_ts_list[0]

        vam_size = []
        decision_result = []
        tmp_chunk_idx = -1
        tmp_chunk = {'chunk_idx': tmp_chunk_idx, 'chunk_meta_data': []}
        tmp_decision = {'chunk_idx': tmp_chunk_idx, 'decision_data': []}
        scale = client_record[client_ts_list[0]]['scale']  # 初始化头部运动轨迹信息（scale、yaw、pitch）
        yaw = client_record[client_ts_list[0]]['yaw']
        pitch = client_record[client_ts_list[0]]['pitch']
        frame_num = len(os.listdir(osp.join(self.work_folder, f"qp{self.quality_list[0]}", 'h264')))
        frame_bar = tqdm(range(self.pre_download_duration * self.video_fps, frame_num), leave=False)
        for frame_idx in frame_bar:  # 从需要开始做质量决策的视频帧开始
            frame_bar.set_description(f"[get vam size] vam_idx={frame_idx}")

            vam_ts = base_ts + frame_idx * 1000.0 / self.video_fps  # 该视频帧的时间戳
            motion_idx = 0
            for i, ts in enumerate(client_ts_list):  # ts：客户端的当前时间
                if ts + self.rtt <= vam_ts - self.decision_delay:  # ts + self.rtt：从客户端传输到服务器端后的当前时间
                    motion_idx = i
                else:
                    break

            scale_ = scale  # 保存上一步的头部运动轨迹信息
            yaw_ = yaw
            pitch_ = pitch
            scale = client_record[client_ts_list[motion_idx]]['scale'] # 获取最新的头部运动轨迹信息
            yaw = client_record[client_ts_list[motion_idx]]['yaw']
            pitch = client_record[client_ts_list[motion_idx]]['pitch']

            if scale != scale_ or yaw != yaw_ or pitch != pitch_:  # 如果头部发生运动
                if len(tmp_chunk['chunk_meta_data']) != 0:
                    tmp_chunk['chunk_meta_data'].insert(0, {'yaw': yaw_, 'pitch': pitch_, 'scale': scale_,
                                                            'motion_ts': client_ts_list[motion_idx - 1]})
                    vam_size.append(deepcopy(tmp_chunk))
                tmp_chunk_idx += 1
                tmp_chunk = {'chunk_idx': tmp_chunk_idx, 'chunk_meta_data': []}

                if len(tmp_decision['decision_data']) != 0:
                    tmp_decision['decision_data'].insert(0, {'yaw': yaw_, 'pitch': pitch_, 'scale': scale_})
                    decision_result.append(deepcopy(tmp_decision))
                tmp_decision = {'chunk_idx': tmp_chunk_idx, 'decision_data': []}

            pw_ts = max(base_ts, vam_ts - self.decision_delay)  # 计算可以进行该视频帧的质量选择的时间戳（>pw_ts后，可以进行质量选择）
            tmp_vam = {"vam_idx": frame_idx, "vam_size_list": []}  # 用以存储当前视频帧的视窗在不同质量下的size（字典）
            for quality in self.quality_list:
                h264_file_path = osp.join(self.work_folder, f"qp{quality}", 'h264', f"{frame_idx + 1}.h264")

                tmp_vam['vam_size_list'].append({'qp': quality, 'vam_size': os.path.getsize(h264_file_path)})
            tmp_chunk['chunk_meta_data'].append(tmp_vam)  # 用以存储当前chunk中的每一视频帧的视窗在不同质量下的size（字典）
            tmp_decision['decision_data'].append({'pw_ts': pw_ts, 'vam_idx': frame_idx, 'qp': self.quality_list[0]})
        frame_bar.close()

        if len(tmp_chunk['chunk_meta_data']) != 0:
            tmp_chunk['chunk_meta_data'].insert(0, {'yaw': yaw, 'pitch': pitch, 'scale': scale,
                                                    'motion_ts': client_ts_list[motion_idx]})
            vam_size.append(deepcopy(tmp_chunk))

        if len(tmp_decision['decision_data']) != 0:
            tmp_decision['decision_data'].insert(0, {'yaw': yaw, 'pitch': pitch, 'scale': scale})
            decision_result.append(deepcopy(tmp_decision))

        json_path = osp.join(self.work_folder, 'video_size.json')
        write_json(vam_size, json_path)
        self.logger.info(f'[write json] path: {json_path}')

        json_path = osp.join(self.opt['project_path'], 'result', self.opt['test_group'], self.opt['video']['origin']['video_name'].split('.')[0],
                             self.opt['method_name'], 'decision.json')
        write_json(decision_result, json_path)
        self.logger.info(f'[write json] path: {json_path}')

    def get_size(self, *args):
        """Read the corresponding file size from video_size.json based on the parameter list"""
        assert self.video_size is not None, f"[get size error] {osp.join(self.work_folder, 'video_size.json')} doesn\'t exist."
        vam_idx, qp_level = args
        while self.video_size[self.chunk_idx]['chunk_meta_data'][-1]['vam_idx'] < vam_idx:
            self.chunk_idx += 1
            assert self.chunk_idx < len(self.video_size), f"[get size error] vam_size={vam_idx} not found!"
        vams = self.video_size[self.chunk_idx]['chunk_meta_data']
        for vam in vams[1:]:
            if int(vam['vam_idx']) == vam_idx:
                qualities = vam['vam_size_list']
                for quality in qualities:
                    if quality['qp'] == qp_level:
                        return quality['vam_size']
                self.logger.error(f"[get size error] vam_idx={vam_idx}, qp_level={qp_level} not found!")
                exit(0)
        self.logger.error(f"[get size error] vam_idx={vam_idx}, qp_level={qp_level} not found!")
        exit(0)

