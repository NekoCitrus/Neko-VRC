import asyncio
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from srv.config_manager import ConfigManager, apply_basic_settings
from srv.waveform import load_waveform_frames
from srv.waveform_library import WaveformLibrary, parse_pulse, FREQUENCIES, DURATIONS

PULSE = 'Dungeonlab+pulse:0,1,16=0,83,3,1,1/0-1,100-1'


class PulseParserTests(unittest.TestCase):
    def test_slider_tables_and_v3_duration(self):
        self.assertEqual(len(FREQUENCIES), 84)
        self.assertEqual(len(DURATIONS), 100)
        self.assertEqual((FREQUENCIES[40], FREQUENCIES[83], DURATIONS[99]), (50, 1000, 3000))
        frames = parse_pulse(PULSE, 'coyote')
        self.assertEqual(frames, ('0A0A0A0A00000000', '0A0A0A0A64646464') * 2)

    def test_speed_is_25ms_samples_and_global_rest_only_once(self):
        body = '0,0,3,1,1/0-1,100-1'
        frames = parse_pulse('Dungeonlab+pulse:3,4,8=' + body + '+section+' + body, 'coyote')
        self.assertEqual(len(frames), 5)  # 8 accelerated samples + 12 rest samples.
        self.assertEqual(frames[0], '0A0A0A0A00640064')
        self.assertEqual(frames[-1], '0A0A0A0A00000000')

    def test_old_exports_without_global_header(self):
        self.assertEqual(parse_pulse('Dungeonlab+pulse:' + PULSE.split('=')[1], 'coyote'), parse_pulse(PULSE, 'coyote'))

    def test_frequency_modes_and_compression(self):
        for mode in (1, 2, 3, 4):
            frames = parse_pulse(f'Dungeonlab+pulse:0,1,16=0,83,3,{mode},1/0-1,100-1', 'coyote')
            frequencies = [bytes.fromhex(frame)[0] for frame in frames]
            self.assertEqual(frequencies[0], 10)
            self.assertEqual(frequencies[-1], 10 if mode == 1 else 240)

    def test_invalid_values_cannot_reach_devices(self):
        bad = [PULSE.replace('100-1', '101-1'), PULSE.replace('100-1', 'nan-1'),
               PULSE.replace(',1,1/', ',1,2/'), PULSE.replace('0,83', '0,84'),
               PULSE.replace('0,1,16=', '0,3,16='), PULSE.replace('0-1,100-1', '0-1')]
        for text in bad:
            with self.subTest(text=text), self.assertRaises((ValueError, TypeError)):
                parse_pulse(text, 'coyote')
        with self.assertRaises(ValueError):
            parse_pulse(PULSE, 'opossum')

    def test_explicit_opossum_frames_are_not_coyote_files(self):
        frames = ['0A0A0A0A00643219']
        text = json.dumps({'device_kind': 'opossum', 'frames': frames})
        self.assertEqual(parse_pulse(text, 'opossum'), tuple(frames))
        with self.assertRaises(ValueError):
            parse_pulse(text, 'coyote')
        with self.assertRaises(ValueError):
            parse_pulse(json.dumps({'device_kind': 'opossum', 'frames': ['F0F0F0F064646464']}), 'opossum')


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lib = WaveformLibrary(self.root)

    def sample(self, name='sample.pulse', text=PULSE):
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_zip_nested_names_duplicate_and_bad_file_continue(self):
        path = self.root / 'waves.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('部位/pulse-样例-1234567.pulse', PULSE)
            archive.writestr('duplicate.pulse', ' \n' + PULSE)
            archive.writestr('invalid.pulse', 'not pulse')
            archive.writestr('../outside.pulse', PULSE)
        report = self.lib.import_paths('coyote', [str(path)])
        self.assertEqual((report['imported'], report['duplicates'], report['failed']), (1, 2, 1))
        self.assertFalse((self.root.parent / 'outside.pulse').exists())
        entry = next(iter(self.lib.data['coyote']['custom'].values()))
        self.assertEqual(entry['name'], '样例')
        self.assertEqual(self.lib.data['opossum']['custom'], {})

    def test_folder_import_and_identical_frames_deduplicate(self):
        self.sample()
        self.sample('another.pulse', PULSE.replace('0-1', '0-0'))
        report = self.lib.import_paths('coyote', [str(self.root)])
        self.assertEqual((report['imported'], report['duplicates']), (1, 1))

    def test_selected_custom_wave_survives_settings_migration_and_restart(self):
        self.lib.import_paths('coyote', [str(self.sample())])
        key = next(iter(self.lib.data['coyote']['custom']))
        self.lib.select('coyote', [key, '纯粹'], ['纯粹'])
        manager = ConfigManager(self.root, self.root)
        settings, basic = manager.load()
        settings['dglab3']['coyote']['channel_a']['depth']['waveform'] = key
        manager.save(settings, basic)
        self.assertNotIn('_waveform_frames', manager.path.read_text(encoding='utf-8'))
        restarted = ConfigManager(self.root, self.root)
        settings, basic = restarted.load()
        self.assertEqual(settings['dglab3']['coyote']['channel_a']['depth']['waveform'], key)
        self.assertEqual(load_waveform_frames(key, 'coyote', settings), parse_pulse(PULSE, 'coyote'))
        self.assertEqual(restarted.waveform_library.catalog()['coyote']['enabled'], [key, '纯粹'])

    def test_manifest_failure_is_atomic_and_active_choices_protected(self):
        original = copy.deepcopy(self.lib.data)
        with self.assertRaises(ValueError):
            self.lib.select('coyote', [], ['纯粹'])
        self.assertEqual(self.lib.data, original)
        with patch('srv.waveform_library.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.lib.import_paths('coyote', [str(self.sample())])
        self.assertEqual(self.lib.data, original)
        self.assertFalse(self.lib.path.exists())

    def test_preview_is_bounded_and_does_not_lose_single_sample_peaks(self):
        self.lib.import_paths('coyote', [str(self.sample())])
        key = next(iter(self.lib.data['coyote']['custom']))
        data = self.lib.preview('coyote', key)
        self.assertEqual(data['duration'], .4)
        self.assertEqual(max(data['amplitudes']), 100)
        self.assertLessEqual(len(data['amplitudes']), 800)

    def test_oversized_archive_member_is_skipped(self):
        path = self.root / 'large.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('large.pulse', 'x' * (1024 * 1024 + 1))
            archive.writestr('valid.pulse', PULSE)
        report = self.lib.import_paths('coyote', [str(path)])
        self.assertEqual((report['imported'], report['failed']), (1, 1))


class CustomRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_selection_hot_applies_without_clearing_input(self):
        import shocking_vrchat as core
        from srv.sps_depth import SPS_PLUG
        with tempfile.TemporaryDirectory() as folder:
            manager = ConfigManager(folder, folder)
            settings, basic = manager.load()
            settings['chatbox']['enable'] = False
            runtime = core.DeviceRuntime(apply_basic_settings(settings, basic))
            runtime.dispatcher = runtime._build_dispatcher()
            self.addCleanup(runtime.chatbox_manager.cleanup, notify=False)
            runtime.router.broadcast_clear_wave = AsyncMock()
            handler = runtime.handlers[0]
            handler.osc_handler('/avatar/parameters/OGB/Pen/Test/PenSelf', (SPS_PLUG, 'PenSelf'), .6)
            await asyncio.sleep(0)
            path = Path(folder) / 'test.pulse'
            path.write_text(PULSE)
            manager.waveform_library.import_paths('coyote', [str(path)])
            settings = manager.waveform_library.attach(settings)
            key = next(iter(settings['_waveform_frames']['coyote']))
            settings['dglab3']['coyote']['channel_a']['depth']['waveform'] = key
            await runtime._apply_settings(apply_basic_settings(settings, basic))
            self.assertEqual(handler.wave_frames, parse_pulse(PULSE, 'coyote'))
            self.assertEqual(handler.current_strength_percentage, .6)
            runtime.router.broadcast_clear_wave.assert_not_awaited()
            await asyncio.gather(*(h.stop_background_jobs() for h in runtime.handlers))
