"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2021 Pupil Labs
Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""

import pathlib
import csv
import os
import typing
import logging
import numpy as np

import OpenGL.GL as gl
from pyglui import ui, pyfontstash
from pyglui.cygl import utils as cygl_utils

import background_helper as bh
import gl_utils
from plugin import Plugin
from pupil_recording import PupilRecording, RecordingInfo
from raw_data_exporter import _Base_Positions_Exporter
import player_methods as pm
import csv_utils

logger = logging.getLogger(__name__)


def glfont_generator():
    glfont = pyfontstash.fontstash.Context()
    glfont.add_font("opensans", ui.get_opensans_font_path())
    glfont.set_color_float((1.0, 1.0, 1.0, 0.8))
    glfont.set_align_string(v_align="right", h_align="top")
    return glfont


def get_limits(data, keys):
    return (
        min([min(data[key]) for key in keys]),
        max([max(data[key]) for key in keys]),
    )


def fuser(data_raw, gyro_error):
    yield "Fusing imu", ()
    fusion = Fusion(gyro_error, 0.00494)
    logger.info("Starting IMU fusion using Madgwick's algorithm")

    for ind, datum in enumerate(data_raw):
        gyro_x, gyro_y, gyro_z, accel_x, accel_y, accel_z = datum
        fusion.update((accel_x, accel_y, accel_z), (gyro_x, gyro_y, gyro_z))
        yield "Fusing imu", ()
        yield "Fused datum", ((fusion.pitch, fusion.roll), ind)

    yield "Fusion complete", ()


def merge_arrays(arr1, arr2):
    NEW_DTYPE = [
        ("gyro_x", "<f4"),
        ("gyro_y", "<f4"),
        ("gyro_z", "<f4"),
        ("accel_x", "<f4"),
        ("accel_y", "<f4"),
        ("accel_z", "<f4"),
        ("pitch", "<f4"),
        ("roll", "<f4"),
    ]
    new_array = np.empty(len(arr1), dtype=NEW_DTYPE).view(np.recarray)

    for key in arr1.dtype.names:
        new_array[key] = arr1[key]

    for key in arr2.dtype.names:
        new_array[key] = arr2[key]

    return new_array


class Fusion(object):
    """
    Class provides sensor fusion estimating pitch and roll using Madgwick's algorithm:
    https://www.x-io.co.uk/res/doc/madgwick_internal_report.pdf
    The update method must be called periodically.
    Original code available at: https://github.com/micropython-IMU/micropython-fusion
    Refactored by Neil M. Thomas (https://github.com/N-M-T) 10.01.2020
    Released under the MIT License (MIT)
    Copyright (c) 2015 Peter Hinch
    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:
    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.
    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
    """

    def __init__(self, gyro_error, sample_dur):
        self.sample_dur = sample_dur  # Time between updates
        self.q = [1.0, 0.0, 0.0, 0.0]  # vector to hold quaternion
        gyro_mean_error = np.radians(gyro_error)
        self.beta = (
            np.sqrt(3.0 / 4.0) * gyro_mean_error
        )  # compute beta (see README in original github page)
        self.pitch = 0
        self.roll = 0

    def update(self, accel, gyro):  # 2-tuples (x, y, z) for accel, gyro
        ax, ay, az = accel  # Units G (but later normalised)
        gx, gy, gz = (np.radians(x) for x in gyro)  # Units deg/s
        q1, q2, q3, q4 = (
            self.q[x] for x in range(4)
        )  # short name local variable for readability
        # Auxiliary variables to avoid repeated arithmetic
        _2q1 = 2 * q1
        _2q2 = 2 * q2
        _2q3 = 2 * q3
        _2q4 = 2 * q4
        _4q1 = 4 * q1
        _4q2 = 4 * q2
        _4q3 = 4 * q3
        _8q2 = 8 * q2
        _8q3 = 8 * q3
        q1q1 = q1 * q1
        q2q2 = q2 * q2
        q3q3 = q3 * q3
        q4q4 = q4 * q4

        # Normalise accelerometer measurement
        norm = np.sqrt(ax * ax + ay * ay + az * az)
        if norm == 0:
            return  # handle NaN
        norm = 1 / norm  # use reciprocal for division
        ax *= norm
        ay *= norm
        az *= norm

        # Gradient decent algorithm corrective step
        s1 = _4q1 * q3q3 + _2q3 * ax + _4q1 * q2q2 - _2q2 * ay
        s2 = (
            _4q2 * q4q4
            - _2q4 * ax
            + 4 * q1q1 * q2
            - _2q1 * ay
            - _4q2
            + _8q2 * q2q2
            + _8q2 * q3q3
            + _4q2 * az
        )
        s3 = (
            4 * q1q1 * q3
            + _2q1 * ax
            + _4q3 * q4q4
            - _2q4 * ay
            - _4q3
            + _8q3 * q2q2
            + _8q3 * q3q3
            + _4q3 * az
        )
        s4 = 4 * q2q2 * q4 - _2q2 * ax + 4 * q3q3 * q4 - _2q3 * ay
        norm = 1 / np.sqrt(
            s1 * s1 + s2 * s2 + s3 * s3 + s4 * s4
        )  # normalise step magnitude
        s1 *= norm
        s2 *= norm
        s3 *= norm
        s4 *= norm

        # Compute rate of change of quaternion
        q_dot1 = 0.5 * (-q2 * gx - q3 * gy - q4 * gz) - self.beta * s1
        q_dot2 = 0.5 * (q1 * gx + q3 * gz - q4 * gy) - self.beta * s2
        q_dot3 = 0.5 * (q1 * gy - q2 * gz + q4 * gx) - self.beta * s3
        q_dot4 = 0.5 * (q1 * gz + q2 * gy - q3 * gx) - self.beta * s4

        # Integrate to yield quaternion
        q1 += q_dot1 * self.sample_dur
        q2 += q_dot2 * self.sample_dur
        q3 += q_dot3 * self.sample_dur
        q4 += q_dot4 * self.sample_dur
        norm = 1 / np.sqrt(
            q1 * q1 + q2 * q2 + q3 * q3 + q4 * q4
        )  # normalise quaternion
        self.q = q1 * norm, q2 * norm, q3 * norm, q4 * norm

        # These are modified to account for Invisible IMU coordinate system and positioning of
        # the IMU within the invisible headset
        self.roll = (
            np.degrees(
                -np.arcsin(2.0 * (self.q[1] * self.q[3] - self.q[0] * self.q[2]))
            )
            + 7
        )
        self.pitch = (
            np.degrees(
                np.arctan2(
                    2.0 * (self.q[0] * self.q[1] + self.q[2] * self.q[3]),
                    self.q[0] * self.q[0]
                    - self.q[1] * self.q[1]
                    - self.q[2] * self.q[2]
                    + self.q[3] * self.q[3],
                )
            )
            + 90
        )


class IMURecording:
    DTYPE_RAW = np.dtype(
        [
            ("gyro_x", "<f4"),
            ("gyro_y", "<f4"),
            ("gyro_z", "<f4"),
            ("accel_x", "<f4"),
            ("accel_y", "<f4"),
            ("accel_z", "<f4"),
        ]
    )

    def __init__(self, path_to_imu_raw: pathlib.Path):
        stem = path_to_imu_raw.stem
        self.path_raw = path_to_imu_raw
        self.path_ts = path_to_imu_raw.with_name(stem + "_timestamps.npy")
        self.load()

    def load(self):
        if not self.path_raw.exists() and self.path_ts.exists():
            self.ts = np.empty(0, dtype=np.float64)
            self.raw = np.empty(0, dtype=self.DTYPE_RAW)
            return

        self.ts = np.load(str(self.path_ts))
        self.raw = np.fromfile(str(self.path_raw), dtype=self.DTYPE_RAW).view(
            np.recarray
        )
        num_ts_during_init = self.ts.size - len(self.raw)
        if num_ts_during_init > 0:
            self.ts = self.ts[num_ts_during_init:]


class IMUTimeline(Plugin):
    """
    plot and export imu data
    export: imu_timeline.csv
    keys:
        imu_timestamp: timestamp of the source image frame
        world_index: associated_frame: closest world video frame
        gyro_x: angular velocity about the x axis in degrees/s
        gyro_y: angular velocity about the y axis in degrees/s
        gyro_z: angular velocity about the z axis in degrees/s
        accel_x: linear acceleration along the x axis in G (9.80665 m/s^2)
        accel_y: linear acceleration along the y axis in G (9.80665 m/s^2)
        accel_z: linear acceleration along the z axis in G (9.80665 m/s^2)
        pitch: orientation expressed as Euler angles
        roll: orientation expressed as Euler angles
    See Pupil docs for relevant coordinate systems
    """

    gyro_error = 50
    should_draw_raw = False
    should_draw_orientation = False

    IMU_PATTERN_RAW = r"^extimu ps(\d+).raw"

    CMAP = {
        "gyro_x": cygl_utils.RGBA(0.12156, 0.46666, 0.70588, 1.0),
        "gyro_y": cygl_utils.RGBA(1.0, 0.49803, 0.05490, 1.0),
        "gyro_z": cygl_utils.RGBA(0.17254, 0.62745, 0.1725, 1.0),
        "accel_x": cygl_utils.RGBA(0.83921, 0.15294, 0.15686, 1.0),
        "accel_y": cygl_utils.RGBA(0.58039, 0.40392, 0.74117, 1.0),
        "accel_z": cygl_utils.RGBA(0.54901, 0.33725, 0.29411, 1.0),
        "pitch": cygl_utils.RGBA(0.12156, 0.46666, 0.70588, 1.0),
        "roll": cygl_utils.RGBA(1.0, 0.49803, 0.05490, 1.0),
    }
    NUMBER_SAMPLES_TIMELINE = 4000
    TIMELINE_LINE_HEIGHT = 16
    icon_chr = chr(0xE922)
    icon_font = "pupil_icons"

    DTYPE_ORIENT = np.dtype(
        [
            ("pitch", "<f4"),
            ("roll", "<f4"),
        ]
    )

    @classmethod
    def parse_pretty_class_name(cls) -> str:
        return "IMU Timeline"

    @classmethod
    def is_available_within_context(cls, g_pool) -> bool:
        if g_pool.app == "player":
            recording = PupilRecording(rec_dir=g_pool.rec_dir)
            meta_info = recording.meta_info
            if (
                meta_info.recording_software_name
                == RecordingInfo.RECORDING_SOFTWARE_NAME_PUPIL_INVISIBLE
            ):
                # Enable in Player only if Pupil Invisible recording
                return True
        return False

    def __init__(self, g_pool):
        super().__init__(g_pool)
        rec = PupilRecording(g_pool.rec_dir)
        imu_files = sorted(rec.files().filter_patterns(self.IMU_PATTERN_RAW))
        imu_recs = [IMURecording(imu_file) for imu_file in imu_files]
        self.bg_task = None
        self.is_invisible_rec = False
        if not len(imu_recs):
            return

        self.is_invisible_rec = True
        self.gyro_timeline = None
        self.accel_timeline = None
        self.orient_timeline = None
        self.glfont_raw = None
        self.glfont_orient = None

        self.data_raw = np.concatenate([rec.raw for rec in imu_recs])
        self.data_ts = np.concatenate([rec.ts for rec in imu_recs])
        self.data_len = len(self.data_raw)
        self.data_orient = np.empty([self.data_len], dtype=self.DTYPE_ORIENT).view(
            np.recarray
        )
        self.gyro_keys = ["gyro_x", "gyro_y", "gyro_z"]
        self.accel_keys = ["accel_x", "accel_y", "accel_z"]
        self.orient_keys = ["pitch", "roll"]

    def init_ui(self):
        if not self.is_invisible_rec:
            return

        self.add_menu()
        self.menu.label = "IMU Timeline"
        self.menu.append(ui.Info_Text("View IMU data and export into .csv file"))
        self.menu.append(
            ui.Info_Text(
                "Use this Plugin to view the IMU data timeline. When enabled, "
                " IMU data will also be exported into a .csv file after running the Raw "
                " Data Exporter Plugin. "
            )
        )
        self.menu.append(
            ui.Info_Text(
                "Orientation is estimated using Madgwick's algorithm. "
                " Madgwick implements a beta value which is related with the "
                " error of the gyroscope. The beta value does not have an "
                " intuitive optimal magnitude. Increasing the beta leads to "
                " faster corrections but with more sensitivity to lateral "
                " accelerations. Read more about Madgwick's algorithm here: "
                " https://www.x-io.co.uk/res/doc/madgwick_internal_report.pdf "
            )
        )

        def set_gyro_error(new_value):
            self.gyro_error = new_value
            self.notify_all({"subject": "madgwick_fusion.should_fuse", "delay": 1.0})

        self.menu.append(
            ui.Switch(
                "should_draw_raw",
                self,
                label="View raw timeline",
                setter=self.on_draw_raw_toggled,
            )
        )
        self.menu.append(
            ui.Switch(
                "should_draw_orientation",
                self,
                label="View orientation timeline",
                setter=self.on_draw_orientation_toggled,
            )
        )
        self.menu.append(
            ui.Slider(
                "gyro_error",
                self,
                min=1,
                step=0.1,
                max=100,
                label="Madgwick's beta",
                setter=set_gyro_error,
            )
        )
        self._fuse()

    def deinit_ui(self):
        if not self.is_invisible_rec:
            return

        if self.should_draw_raw:
            self.g_pool.user_timelines.remove(self.gyro_timeline)
            self.g_pool.user_timelines.remove(self.accel_timeline)
            del self.gyro_timeline
            del self.accel_timeline
            del self.glfont_raw

        if self.should_draw_orientation:
            self.g_pool.user_timelines.remove(self.orient_timeline)
            del self.glfont_orient

        self.cleanup()
        self.remove_menu()

    def cleanup(self):
        if self.bg_task:
            self.bg_task.cancel()
            self.bg_task = None

    def _fuse(self):
        """
        Fuse imu data
        """
        if self.bg_task:
            self.bg_task.cancel()

        generator_args = (
            self.data_raw,
            self.gyro_error,
        )

        self.bg_task = bh.IPC_Logging_Task_Proxy("Fusion", fuser, args=generator_args)

    def recent_events(self, events):
        if self.bg_task:
            for progress, task_data in self.bg_task.fetch():
                self.status = progress
                if task_data:
                    current_progress = task_data[1] / self.data_len
                    self.menu_icon.indicator_stop = current_progress
                    self.data_orient["pitch"][task_data[1]] = task_data[0][0]
                    self.data_orient["roll"][task_data[1]] = task_data[0][1]

            if self.bg_task.completed:
                self.status = "{} imu data fused"
                self.bg_task = None
                self.menu_icon.indicator_stop = 0.0
                if self.should_draw_orientation:
                    # redraw new orientation data
                    self.remove_orientation()
                    self.draw_orientation()

    def on_draw_raw_toggled(self, new_value):
        self.should_draw_raw = new_value
        if self.should_draw_raw:
            self.draw_raw()
        else:
            self.remove_raw()

    def on_draw_orientation_toggled(self, new_value):
        # check that data is fused
        if self.bg_task:
            logger.warning("Running Madgwick's algorithm")
            return

        self.should_draw_orientation = new_value
        if self.should_draw_orientation:
            self.draw_orientation()
        else:
            self.remove_orientation()

    def draw_raw(self):
        self.gyro_timeline = ui.Timeline(
            "gyro",
            self.draw_raw_gyro,
            self.draw_legend_gyro,
            self.TIMELINE_LINE_HEIGHT * 3,
        )
        self.accel_timeline = ui.Timeline(
            "accel",
            self.draw_raw_accel,
            self.draw_legend_accel,
            self.TIMELINE_LINE_HEIGHT * 3,
        )
        self.g_pool.user_timelines.append(self.gyro_timeline)
        self.g_pool.user_timelines.append(self.accel_timeline)
        self.glfont_raw = glfont_generator()

    def draw_orientation(self):
        self.orient_timeline = ui.Timeline(
            "orientation",
            self.draw_orient,
            self.draw_legend_orient,
            self.TIMELINE_LINE_HEIGHT * 2,
        )
        self.g_pool.user_timelines.append(self.orient_timeline)
        self.glfont_orient = glfont_generator()

    def remove_raw(self):
        self.g_pool.user_timelines.remove(self.gyro_timeline)
        self.g_pool.user_timelines.remove(self.accel_timeline)
        del self.gyro_timeline
        del self.accel_timeline
        del self.glfont_raw

    def remove_orientation(self):
        self.g_pool.user_timelines.remove(self.orient_timeline)
        del self.glfont_orient

    def draw_raw_gyro(self, width, height, scale):
        y_limits = get_limits(self.data_raw, self.gyro_keys)
        self._draw_grouped(
            self.data_raw, self.gyro_keys, y_limits, width, height, scale
        )

    def draw_raw_accel(self, width, height, scale):
        y_limits = get_limits(self.data_raw, self.accel_keys)
        self._draw_grouped(
            self.data_raw, self.accel_keys, y_limits, width, height, scale
        )

    def draw_orient(self, width, height, scale):
        y_limits = get_limits(self.data_orient, self.orient_keys)
        self._draw_grouped(
            self.data_orient, self.orient_keys, y_limits, width, height, scale
        )

    def _draw_grouped(self, data, keys, y_limits, width, height, scale):
        ts_min = self.g_pool.timestamps[0]
        ts_max = self.g_pool.timestamps[-1]
        data_raw = data[keys]
        with gl_utils.Coord_System(ts_min, ts_max, *y_limits):
            for key in keys:
                data_keyed = data_raw[key]
                points = list(zip(self.data_ts, data_keyed))
                cygl_utils.draw_points(points, size=1.5 * scale, color=self.CMAP[key])

    def draw_legend_gyro(self, width, height, scale):
        self._draw_legend_grouped(self.gyro_keys, width, height, scale, self.glfont_raw)

    def draw_legend_accel(self, width, height, scale):
        self._draw_legend_grouped(
            self.accel_keys, width, height, scale, self.glfont_raw
        )

    def draw_legend_orient(self, width, height, scale):
        self._draw_legend_grouped(
            self.orient_keys, width, height, scale, self.glfont_orient
        )

    def _draw_legend_grouped(self, labels, width, height, scale, glfont):
        glfont.set_size(self.TIMELINE_LINE_HEIGHT * 0.8 * scale)
        pad = width * 2 / 3
        for label in labels:
            color = self.CMAP[label]
            glfont.draw_text(width, 0, label)

            cygl_utils.draw_polyline(
                [
                    (pad, self.TIMELINE_LINE_HEIGHT / 2),
                    (width / 4, self.TIMELINE_LINE_HEIGHT / 2),
                ],
                color=color,
                line_type=gl.GL_LINES,
                thickness=4.0 * scale,
            )
            gl.glTranslatef(0, self.TIMELINE_LINE_HEIGHT * scale, 0)

    def on_notify(self, notification):
        if not self.is_invisible_rec:
            return
        if notification["subject"] == "madgwick_fusion.should_fuse":
            self._fuse()
        elif notification["subject"] == "should_export":
            if not self.bg_task:
                self.export_data(notification["ts_window"], notification["export_dir"])
            else:
                logger.warning("Running Madgwick's algorithm")

    def export_data(self, export_window, export_dir):
        for_export = merge_arrays(self.data_raw, self.data_orient)

        imu_bisector = Imu_Bisector(for_export, self.data_ts)
        imu_exporter = Imu_Exporter()
        imu_exporter.csv_export_write(
            imu_bisector=imu_bisector,
            timestamps=self.g_pool.timestamps,
            export_window=export_window,
            export_dir=export_dir,
        )


class Imu_Bisector(pm.Bisector):
    """Stores data with associated timestamps, both sorted by the timestamp;
    subclassed to avoid casting to object and losing dtypes for recarrays"""

    def __init__(self, data=(), data_ts=()):
        if len(data) != len(data_ts):
            raise ValueError(
                (
                    "Each element in 'data' requires a corresponding"
                    " timestamp in `data_ts`"
                )
            )

        elif not len(data):
            self.data = np.array([], dtype=object)
            self.data_ts = np.array([])
            self.sorted_idc = []

        else:
            self.data_ts = data_ts
            self.data = data

            # Find correct order once and reorder both lists in-place
            self.sorted_idc = np.argsort(self.data_ts)
            self.data_ts = self.data_ts[self.sorted_idc]
            self.data = self.data[self.sorted_idc]


class Imu_Exporter(_Base_Positions_Exporter):
    @classmethod
    def csv_export_filename(cls) -> str:
        return "imu_data.csv"

    @classmethod
    def csv_export_labels(cls) -> typing.Tuple[csv_utils.CSV_EXPORT_LABEL_TYPE, ...]:
        return (
            "imu_timestamp",
            "world_index",
            "gyro_x",
            "gyro_y",
            "gyro_z",
            "accel_x",
            "accel_y",
            "accel_z",
            "pitch",
            "roll",
        )

    @classmethod
    def dict_export(
        cls, raw_value: csv_utils.CSV_EXPORT_RAW_TYPE, world_ts: float, world_index: int
    ) -> dict:
        try:
            imu_timestamp = str(world_ts)
            gyro_x = raw_value["gyro_x"]
            gyro_y = raw_value["gyro_y"]
            gyro_z = raw_value["gyro_z"]
            accel_x = raw_value["accel_x"]
            accel_y = raw_value["accel_y"]
            accel_z = raw_value["accel_z"]
            pitch = raw_value["pitch"]
            roll = raw_value["roll"]
        except KeyError:
            imu_timestamp = None
            gyro_x = None
            gyro_y = None
            gyro_z = None
            accel_x = None
            accel_y = None
            accel_z = None
            pitch = None
            roll = None

        return {
            "imu_timestamp": imu_timestamp,
            "world_index": world_index,
            "gyro_x": gyro_x,
            "gyro_y": gyro_y,
            "gyro_z": gyro_z,
            "accel_x": accel_x,
            "accel_y": accel_y,
            "accel_z": accel_z,
            "pitch": pitch,
            "roll": roll,
        }

    def csv_export_write(self, imu_bisector, timestamps, export_window, export_dir):
        export_file = type(self).csv_export_filename()
        export_path = os.path.join(export_dir, export_file)

        export_section = imu_bisector.init_dict_for_window(export_window)
        export_world_idc = pm.find_closest(timestamps, export_section["data_ts"])

        with open(export_path, "w", encoding="utf-8", newline="") as csvfile:
            csv_header = type(self).csv_export_labels()
            dict_writer = csv.DictWriter(csvfile, fieldnames=csv_header)
            dict_writer.writeheader()

            for d_raw, wts, idx in zip(
                export_section["data"], export_section["data_ts"], export_world_idc
            ):
                dict_row = type(self).dict_export(
                    raw_value=d_raw, world_ts=wts, world_index=idx
                )
                dict_writer.writerow(dict_row)

        logger.info(f"Created '{export_file}' file.")
