"""Consumer-facing compatibility boundaries; no external tools or media required."""
import copy
import threading
import unittest

from backend import _is_transient_io_error, _retry_transient
from bitrate import _split_ffprobe_diagnostics, apply
from engine import assess
from tv_profiles import get_profile, validate


def metadata():
    return {'media': {'track': [
        {'@type': 'General', 'Format': 'Matroska'},
        {'@type': 'Video', 'Format': 'AVC', 'Format_Profile': 'High@L4', 'Width': '1920', 'Height': '816',
         'FrameRate': '25', 'BitDepth': '8', 'ChromaSubsampling': '4:2:0', 'BitRate': '8000000'},
        {'@type': 'Audio', 'Format': 'E-AC-3', 'Channels': '2', 'SamplingRate': '48000'},
    ]}}


class CompatibilityTests(unittest.TestCase):
    def test_subtitle_typography_does_not_change_playback_verdict(self):
        media = metadata()
        media['media']['track'].append({'@type': 'Text', 'Format': 'ASS', 'CodecID': 'S_TEXT/ASS'})
        result = assess('anime.mkv', media)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['subtitles'][0]['status'], 'info')

    def test_dts_usb_and_network_are_different_contracts(self):
        media = metadata()
        media['media']['track'][2]['Format'] = 'DTS'
        self.assertEqual(assess('movie.mkv', media, 'usb')['status'], 'ok')
        self.assertEqual(assess('movie.mkv', media, 'network')['status'], 'fail')

    def test_unsupported_audio_with_working_alternative_is_not_total_failure(self):
        media = metadata()
        media['media']['track'].append({'@type': 'Audio', 'Format': 'Opus', 'Default': 'Yes', 'Channels': '2', 'SamplingRate': '48000'})
        result = assess('movie.mkv', media)
        self.assertEqual(result['status'], 'warn')
        self.assertEqual([t['status'] for t in result['audio']], ['ok', 'fail'])

    def test_ten_bit_avc_is_rejected_but_hevc_main10_is_allowed(self):
        media = metadata()
        video = media['media']['track'][1]
        video.update(BitDepth='10', Format_Profile='High 10@L4')
        self.assertEqual(assess('anime.mkv', media)['status'], 'fail')
        video.update(Format='HEVC', Format_Profile='Main 10@L4')
        self.assertEqual(assess('anime.mkv', media)['status'], 'ok')

    def test_late_peak_over_limit_is_not_hidden_by_low_average(self):
        result = assess('movie.mkv', metadata())
        measured = {'complete': True, 'average_mbps': 2, 'peak_mbps': 43, 'peak_second': 129, 'issues': [],
                    'streams': [{'type': 'video', 'average_mbps': 1.8, 'peak_mbps': 42, 'peak_second': 129}]}
        apply(result, measured, 'usb')
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['video'][0]['status'], 'fail')

    def test_media_info_only_check_does_not_require_full_bitrate_measurement(self):
        result = assess('movie.mkv', metadata())
        apply(result, None, 'usb')
        self.assertEqual(result['status'], 'ok')
        self.assertIn('MediaInfo', result['summary'])

    def test_custom_profile_can_enable_codec_lg_does_not_support(self):
        media = metadata()
        media['media']['track'][1].update(Format='AV1', Format_Profile='Main')
        self.assertEqual(assess('movie.mkv', media)['status'], 'fail')
        profile = get_profile()
        profile.update(id='test-other-tv', name='Different TV')
        profile['unsupported_video'].remove('AV1')
        profile['containers']['mkv']['video'].append('AV1')
        profile['video_rules']['AV1'] = copy.deepcopy(profile['video_rules']['AVC'])
        profile['video_rules']['AV1'].update(profiles=['Main'], max_level_fhd=None, max_level_uhd=None)
        self.assertEqual(assess('movie.mkv', media, profile=validate(profile))['status'], 'ok')

    def test_nonblocking_usb_metadata_gap_is_shown_as_note_not_failure(self):
        media = metadata()
        media['media']['track'][1].pop('Format_Profile')
        result = assess('movie.mkv', media)
        self.assertEqual(result['status'], 'warn')
        apply(result, {
            'complete': True,
            'average_mbps': 4,
            'peak_mbps': 5,
            'peak_second': 2,
            'issues': [],
            'streams': [{'type': 'video', 'average_mbps': 3.8, 'peak_mbps': 5, 'peak_second': 2}],
        }, 'usb')
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['technical_notes'])
        self.assertEqual(result['video'][0]['status'], 'ok')

    def test_benign_ffprobe_reorder_message_is_technical_only(self):
        relevant, technical = _split_ffprobe_diagnostics(
            '[h264 @ 000002d5ddade900] Increasing reorder buffer to 2'
        )
        self.assertEqual(relevant, '')
        self.assertEqual(technical, ['[h264 @ 000002d5ddade900] Increasing reorder buffer to 2'])
        result = assess('movie.mkv', metadata())
        apply(result, {
            'complete': True,
            'average_mbps': 8.5,
            'peak_mbps': 20.92,
            'peak_second': 1020,
            'issues': [],
            'diagnostics': technical,
            'streams': [{'type': 'video', 'average_mbps': 8.1, 'peak_mbps': 20.92, 'peak_second': 1020}],
        }, 'usb')
        self.assertEqual(result['status'], 'ok')
        self.assertIn('FFprobe: ' + technical[0], result['technical_notes'])

    def test_invalid_numeric_limit_is_rejected_instead_of_disabling_check(self):
        profile = get_profile()
        profile['video_rules']['AVC']['max_mbps_fhd'] = float('nan')
        with self.assertRaises(ValueError):
            validate(profile)

    def test_winerror_50_retries_and_preserves_final_failure(self):
        class WinError50(OSError):
            winerror = 50

        calls = []

        def flaky():
            calls.append(len(calls))
            if len(calls) < 3:
                raise WinError50(50, 'The request is not supported')
            return 'ok'

        self.assertEqual(_retry_transient(flaky, threading.Event(), delay=0), 'ok')
        self.assertEqual(len(calls), 3)

        def always_fails():
            raise WinError50(50, 'The request is not supported')

        with self.assertRaises(WinError50):
            _retry_transient(always_fails, threading.Event(), delay=0)


if __name__ == '__main__':
    unittest.main()
