import unittest
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / 'app' / 'templates' / 'index.html'
TEXT = HTML.read_text(encoding='utf-8')


class FunctionalShellRegressionTests(unittest.TestCase):
    def test_restores_main_navigation_views(self):
        for view in ['home','new','runs','results','exports','sources','settings']:
            self.assertIn(f'id="view-{view}"', TEXT)

    def test_restores_new_analysis_workflow(self):
        for token in ['New analysis','Research brief','Smart Search','Sample size','Review analysis']:
            self.assertIn(token, TEXT)

    def test_restores_source_actor_settings(self):
        self.assertIn('Sources & Actors', TEXT)
        self.assertIn('xquik/x-tweet-scraper', TEXT)
        self.assertIn('epctex/tiktok-search-scraper', TEXT)

    def test_report_language_is_separate_field(self):
        self.assertIn("reportLang:'English'", TEXT)
        self.assertIn("reportLang:'Report language'", TEXT)

    def test_direct_file_mode_does_not_fake_backend(self):
        self.assertIn("location.protocol==='file:'", TEXT)
        self.assertIn("signalyth:run-confirmed", TEXT)

    def test_client_profiles_persist_in_local_storage(self):
        self.assertIn('sig_client_profiles', TEXT)
        self.assertIn('persistClientProfiles', TEXT)

    def test_client_logo_upload_exists(self):
        self.assertIn('id="clientProfileLogo"', TEXT)
        self.assertIn('accept="image/png,image/jpeg,image/webp,image/svg+xml"', TEXT)

    def test_agency_branding_upload_exists(self):
        self.assertIn('id="agencyLogo"', TEXT)
        self.assertIn('sig_agency_branding', TEXT)

    def test_powered_by_signalyth_is_discreet_setting(self):
        self.assertIn('Powered by SIGNALYTH', TEXT)
        self.assertIn('id="agencyPowered"', TEXT)

    def test_client_field_can_use_saved_profiles(self):
        self.assertIn('list="clientProfileList"', TEXT)
        self.assertIn('clientDatalist()', TEXT)

    def test_static_marketing_page_regression_is_gone(self):
        self.assertNotIn('WOW Presentation +<br>Decision Intelligence', TEXT)
        self.assertNotIn('Jaw-dropping, never', TEXT)

    def test_version_title_marks_restoration(self):
        self.assertIn('<title>SIGNALYTH v1.8.4</title>', TEXT)


if __name__ == '__main__':
    unittest.main()
