"""Small ctypes binding to the GOS-installed librealsense; no SDK upgrade."""
import ctypes as C
import os
import time


class Intrinsics(C.Structure):
    _fields_ = [('width', C.c_int), ('height', C.c_int), ('ppx', C.c_float),
                ('ppy', C.c_float), ('fx', C.c_float), ('fy', C.c_float),
                ('model', C.c_int), ('coeffs', C.c_float * 5)]


class Extrinsics(C.Structure):
    _fields_ = [('rotation', C.c_float * 9), ('translation', C.c_float * 3)]


class RealSense:
    def __init__(self, width=640, height=480, fps=15, library=None):
        self.width, self.height, self.fps = width, height, fps
        path = library or os.environ.get('REALSENSE_LIBRARY', '/opt/realsense-rsusb-2.51.1/lib/librealsense2.so')
        self.lib = C.CDLL(path)
        self.error = C.c_void_p
        self._bind()
        self.ctx = self.call('rs2_create_context', self.call('rs2_get_api_version'))
        self.pipe = self.call('rs2_create_pipeline', self.ctx)
        self.config = self.call('rs2_create_config')
        self.profile = None
        # rs2_stream depth=1/color=2; rs2_format Z16=1/RGB8=5.
        self.call('rs2_config_enable_stream', self.config, 1, -1, width, height, 1, fps)
        self.call('rs2_config_enable_stream', self.config, 2, -1, width, height, 5, fps)
        self.profile = self.call('rs2_pipeline_start_with_config', self.pipe, self.config)
        self.metadata = {'library': path, 'api_version': self.call('rs2_get_api_version'),
                         'width': width, 'height': height, 'fps': fps, 'streams': {}}
        dev = self.call('rs2_pipeline_profile_get_device', self.profile)
        try:
            for name, enum in [('name', 0), ('serial_number', 1), ('firmware_version', 2), ('usb_type_descriptor', 9)]:
                if self.call('rs2_supports_device_info', dev, enum):
                    self.metadata[name] = self.call('rs2_get_device_info', dev, enum).decode()
            # Use device hardware clocks, avoiding the SDK's wall-clock fitting
            # while GOS has an incorrect calendar clock. Enum verified in 2.51.1.
            sensors = self.call('rs2_query_sensors', dev)
            try:
                for i in range(self.call('rs2_get_sensors_count', sensors)):
                    sensor = self.call('rs2_create_sensor', sensors, i)
                    try:
                        if self.call('rs2_supports_option', sensor, 53):
                            self.call('rs2_set_option', sensor, 53, 0.0)
                    finally:
                        self.lib.rs2_delete_sensor(sensor)
            finally:
                self.lib.rs2_delete_sensor_list(sensors)
            self.metadata['global_time_enabled'] = False
        finally:
            self.lib.rs2_delete_device(dev)

    def _bind(self):
        P, I, E = C.c_void_p, C.c_int, C.POINTER(C.c_void_p)
        specs = {
            'rs2_get_api_version': (I, []), 'rs2_create_context': (P, [I]),
            'rs2_create_pipeline': (P, [P]), 'rs2_create_config': (P, []),
            'rs2_config_enable_stream': (None, [P, I, I, I, I, I, I]),
            'rs2_pipeline_start_with_config': (P, [P, P]),
            'rs2_pipeline_wait_for_frames': (P, [P, C.c_uint]),
            'rs2_embedded_frames_count': (I, [P]), 'rs2_extract_frame': (P, [P, I]),
            'rs2_get_frame_stream_profile': (P, [P]),
            'rs2_get_stream_profile_data': (None, [P] + [C.POINTER(I)] * 5),
            'rs2_get_frame_timestamp': (C.c_double, [P]),
            'rs2_get_frame_timestamp_domain': (I, [P]),
            'rs2_get_frame_number': (C.c_ulonglong, [P]),
            'rs2_get_frame_data': (P, [P]),
            'rs2_get_frame_width': (I, [P]), 'rs2_get_frame_height': (I, [P]),
            'rs2_get_frame_stride_in_bytes': (I, [P]),
            'rs2_get_video_stream_intrinsics': (None, [P, C.POINTER(Intrinsics)]),
            'rs2_get_extrinsics': (None, [P, P, C.POINTER(Extrinsics)]),
            'rs2_depth_frame_get_units': (C.c_float, [P]),
            'rs2_pipeline_profile_get_device': (P, [P]),
            'rs2_supports_device_info': (I, [P, I]), 'rs2_get_device_info': (C.c_char_p, [P, I]),
            'rs2_pipeline_stop': (None, [P]),
            'rs2_query_sensors': (P, [P]), 'rs2_get_sensors_count': (I, [P]),
            'rs2_create_sensor': (P, [P, I]), 'rs2_supports_option': (I, [P, I]),
            'rs2_set_option': (None, [P, I, C.c_float]),
        }
        for name, (result, args) in specs.items():
            fn = getattr(self.lib, name)
            fn.restype = result
            fn.argtypes = args + [E]
        self.lib.rs2_get_error_message.restype = C.c_char_p
        self.lib.rs2_get_error_message.argtypes = [P]
        for name in ['rs2_free_error', 'rs2_release_frame', 'rs2_delete_pipeline',
                     'rs2_delete_config', 'rs2_delete_context', 'rs2_delete_pipeline_profile', 'rs2_delete_device',
                     'rs2_delete_sensor', 'rs2_delete_sensor_list']:
            getattr(self.lib, name).argtypes = [P]
            getattr(self.lib, name).restype = None

    def call(self, name, *args):
        err = C.c_void_p()
        result = getattr(self.lib, name)(*args, C.byref(err))
        if err.value:
            message = self.lib.rs2_get_error_message(err).decode()
            self.lib.rs2_free_error(err)
            raise RuntimeError(name + ': ' + message)
        return result

    def read(self):
        import numpy as np
        frames = self.call('rs2_pipeline_wait_for_frames', self.pipe, 1500)
        recv_ns = time.monotonic_ns()
        result, profiles, owned = {}, {}, []
        try:
            for index in range(self.call('rs2_embedded_frames_count', frames)):
                frame = self.call('rs2_extract_frame', frames, index)
                owned.append(frame)
                profile = self.call('rs2_get_frame_stream_profile', frame)
                values = [C.c_int() for _ in range(5)]
                self.call('rs2_get_stream_profile_data', profile, *[C.byref(x) for x in values])
                stream, fmt, _, _, fps = [x.value for x in values]
                if stream not in (1, 2):
                    continue
                name = 'depth' if stream == 1 else 'color'
                if fmt != (1 if stream == 1 else 5):
                    raise RuntimeError('Unexpected camera format: %s' % fmt)
                w = self.call('rs2_get_frame_width', frame)
                h = self.call('rs2_get_frame_height', frame)
                stride = self.call('rs2_get_frame_stride_in_bytes', frame)
                raw = C.string_at(self.call('rs2_get_frame_data', frame), stride * h)
                rows = np.frombuffer(raw, dtype=np.uint8).reshape(h, stride)
                data = rows[:, :w * (2 if stream == 1 else 3)].copy()
                data = data.view('<u2').reshape(h, w) if stream == 1 else data.reshape(h, w, 3)
                item = {'data': data, 'timestamp_ms': self.call('rs2_get_frame_timestamp', frame),
                        'timestamp_domain': self.call('rs2_get_frame_timestamp_domain', frame),
                        'frame_number': self.call('rs2_get_frame_number', frame), 'receive_ns': recv_ns}
                result[name] = item
                profiles[name] = profile
                if name not in self.metadata['streams']:
                    intr = Intrinsics()
                    self.call('rs2_get_video_stream_intrinsics', profile, C.byref(intr))
                    self.metadata['streams'][name] = {f: (list(getattr(intr, f)) if f == 'coeffs' else getattr(intr, f)) for f, _ in intr._fields_}
                    self.metadata['streams'][name]['fps'] = fps
                if stream == 1:
                    self.metadata['depth_scale_m'] = self.call('rs2_depth_frame_get_units', frame)
            if len(profiles) == 2 and 'depth_to_color' not in self.metadata:
                ext = Extrinsics()
                self.call('rs2_get_extrinsics', profiles['depth'], profiles['color'], C.byref(ext))
                self.metadata['depth_to_color'] = {'rotation_column_major': list(ext.rotation), 'translation_m': list(ext.translation)}
            return result
        finally:
            for frame in owned:
                self.lib.rs2_release_frame(frame)
            self.lib.rs2_release_frame(frames)

    def close(self):
        if self.profile:
            self.call('rs2_pipeline_stop', self.pipe)
            self.lib.rs2_delete_pipeline_profile(self.profile)
            self.profile = None
        self.lib.rs2_delete_config(self.config)
        self.lib.rs2_delete_pipeline(self.pipe)
        self.lib.rs2_delete_context(self.ctx)

