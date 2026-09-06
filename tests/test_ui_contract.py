import unittest
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')


class UiContractTests(unittest.TestCase):
    def test_foundation_contains_no_fake_client_or_demo_analysis(self):
        self.assertNotIn('Eurojackpot', HTML)
        self.assertNotIn('OPAP', HTML)
        self.assertNotIn('workspace ready', HTML.lower())
        self.assertNotIn('demo run', HTML.lower())

    def test_sample_size_modes_remain_present(self):
        for token in ("quick:'Quick'", "standard:'Standard'", "deep:'Deep'", "custom:'Custom'", "perSource:'Per source'", "automatic:'Automatic distribution'"):
            self.assertIn(token, HTML)

    def test_run_lifecycle_ui_is_bilingual(self):
        for en, el in (
            ("waitingWorker:'Waiting for an available collection worker'", "waitingWorker:'Περιμένει διαθέσιμο collection worker'"),
            ("collectionCompleted:'Collection completed'", "collectionCompleted:'Η συλλογή ολοκληρώθηκε'"),
            ("cancelRun:'Cancel run'", "cancelRun:'Ακύρωση run'"),
        ):
            self.assertIn(en, HTML)
            self.assertIn(el, HTML)
        self.assertIn('function currentStepText(x)', HTML)

    def test_confirm_starts_background_api_contract(self):
        self.assertIn("fetch('/api/runs?start=true'", HTML)
        self.assertIn("fetch('/api/runs/'+encodeURIComponent(runId)", HTML)
        self.assertIn("+'/cancel'", HTML)

    def test_run_output_is_escaped_before_insertion(self):
        self.assertIn("esc(x.topic||'—')", HTML)
        self.assertIn("esc(x.run_id)", HTML)
        self.assertIn("esc(d.fatal_error)", HTML)

    def test_cleaning_relevance_ui_is_bilingual_and_not_demo_filled(self):
        for token in (
            "cleaningTitle:'Cleaning & Relevance'",
            "dataQuality:'Data quality'",
            "cleaningTitle:'Cleaning & Relevance'",
            "dataQuality:'Ποιότητα δεδομένων'",
            "reviewQueue:'Ουρά ανθρώπινου ελέγχου'",
        ):
            self.assertIn(token, HTML)
        self.assertIn('function cleaningSummaryHtml', HTML)
        self.assertIn('function openReviewQueue', HTML)

    def test_results_page_uses_real_api_data_only(self):
        self.assertIn("fetch('/api/runs'", HTML)
        self.assertIn("x.cleaning?.status==='succeeded'", HTML)
        self.assertNotIn('91/100', HTML)
        self.assertNotIn('590 trusted', HTML.lower())

    def test_human_review_actions_use_api_and_escape_content(self):
        self.assertIn("+'/review/'+encodeURIComponent(rec.dataset.record)", HTML)
        self.assertIn("esc((x.text||'').slice(0,360))", HTML)
        self.assertIn("data-action=\"keep\"", HTML)
        self.assertIn("data-action=\"exclude\"", HTML)

    def test_ai_analysis_ui_is_bilingual_real_api_only(self):
        for token in (
            "aiTitle:'AI Analysis'",
            "aiReviewQueue:'AI review queue'",
            "aiReviewQueue:'Ουρά AI ελέγχου'",
            "aiAnalyzing:'Semantic relevance, sentiment, emotion, topic, narrative and sarcasm analysis'",
        ):
            self.assertIn(token, HTML)
        self.assertIn('function aiSummaryHtml', HTML)
        self.assertIn('function openAIReviewQueue', HTML)
        self.assertIn("+'/analysis/review'", HTML)
        self.assertIn("+'/analysis/review/'+encodeURIComponent(rec.dataset.aiRecord)", HTML)
        self.assertIn("esc((x.text||'').slice(0,360))", HTML)

    def test_intelligence_ui_is_bilingual_and_real_api_only(self):
        for token in (
            "intelligenceTitle:'Intelligence Engine'",
            "brandReputation:'Brand Reputation'",
            "effectiveVoices:'Effective voices'",
            "effectiveVoices:'Αποτελεσματικές ανεξάρτητες φωνές'",
        ):
            self.assertIn(token, HTML)
        self.assertIn('function intelligenceSummaryHtml', HTML)
        self.assertIn("d.intelligence?.summary", HTML)
        self.assertNotIn('Brand Reputation · 72', HTML)
        self.assertNotIn('87/100 evidence confidence', HTML.lower())


    def test_automatic_investigations_ui_is_bilingual_and_real_api_only(self):
        for token in (
            "investigationsTitle:'Automatic Investigations'",
            "highPriority:'High priority'",
            "highPriority:'Υψηλής προτεραιότητας'",
            "indicatorCoverage:'Indicators examined'",
            "indicatorCoverage:'Δείκτες που εξετάστηκαν'",
            "causalGuardrail:'Association ≠ proven causality'",
            "causalGuardrail:'Συσχέτιση ≠ αποδεδειγμένη αιτιότητα'",
        ):
            self.assertIn(token, HTML)
        self.assertIn('function investigationsSummaryHtml', HTML)
        self.assertIn("d.investigations?.summary", HTML)
        self.assertIn("case 'automatic_investigations'", HTML)
        self.assertIn("case 'investigations_completed'", HTML)
        self.assertNotIn('37 accounts generated 124', HTML.lower())
        self.assertNotIn('investigations · 8', HTML.lower())

    def test_ui_contains_no_fake_ai_metrics(self):
        self.assertNotIn('92% positive', HTML.lower())
        self.assertNotIn('anger spike detected', HTML.lower())
        self.assertNotIn('fake ai result', HTML.lower())


if __name__ == '__main__':
    unittest.main()

class Step7UiContractTests(unittest.TestCase):
    def test_step7_ui_is_bilingual(self):
        for token in (
            "chartsTitle:'Charts & Dashboard'",
            "openDashboard:'Open dashboard'",
            "openDashboard:'Άνοιγμα dashboard'",
            "visualizationsRunning:'Building evidence-linked charts and dashboard'",
            "visualizationsRunning:'Δημιουργία evidence-linked charts και dashboard'",
        ):
            self.assertIn(token, HTML)

    def test_dashboard_uses_real_api_only(self):
        self.assertIn("+'/dashboard'", HTML)
        self.assertIn('function openDashboard(runId)', HTML)
        self.assertIn('function renderChartBody(c)', HTML)
        self.assertNotIn('Eurojackpot', HTML)
        self.assertNotIn('61.25', HTML)
        self.assertNotIn('Winning dream', HTML)

    def test_dashboard_escapes_dynamic_evidence(self):
        for token in (
            "esc(x.excerpt||'')",
            "esc(x.author||'Unknown')",
            "esc(chartLang(x.question)||x.type||'Investigation')",
            "esc(ctx.topic||'—')",
        ):
            self.assertIn(token, HTML)

    def test_dashboard_does_not_multiply_step6_confidence_by_100(self):
        self.assertIn("fmtNum(Number(x.confidence.score),0)", HTML)
        self.assertNotIn("Number(x.confidence.score)*100", HTML)

    def test_step7_lifecycle_codes_are_visible(self):
        self.assertIn("case 'visualization_engine':return t('visualizationsRunning')", HTML)
        self.assertIn("case 'visualizations_completed':return t('visualizationsCompleted')", HTML)
        self.assertIn("case 'visualizations_failed':return t('visualizationsFailed')", HTML)

    def test_results_and_run_details_surface_real_visualization_state(self):
        self.assertIn('function visualizationsSummaryHtml', HTML)
        self.assertIn('d.visualizations?.summary', HTML)
        self.assertIn("x.visualizations?.summary?t('chartsTitle')", HTML)

    def test_no_raster_chart_library_dependency(self):
        self.assertNotIn('chart.js', HTML.lower())
        self.assertNotIn('plotly', HTML.lower())
        self.assertIn('<svg class="trend-svg"', HTML)

class Step8UiContractTests(unittest.TestCase):
    def test_exports_ui_is_bilingual_and_real_api_only(self):
        for token in (
            "generateExports:'Generate presentation & exports'",
            "generateExports:'Δημιουργία παρουσίασης & exports'",
            "presentationEngine:'Presentation Engine'",
            "presentationCopy:'Generate the client PowerPoint",
        ):
            self.assertIn(token, HTML)
        self.assertIn("+'/exports'", HTML)
        self.assertIn('data-build-exports', HTML)

    def test_exports_page_does_not_embed_fake_downloads(self):
        self.assertNotIn('Eurojackpot_August_2026.pptx', HTML)
        self.assertNotIn('download sample report', HTML.lower())
        self.assertNotIn('fake powerpoint', HTML.lower())

    def test_exports_downloads_are_manifest_driven(self):
        self.assertIn('exp?.manifest?.files', HTML)
        self.assertIn("/exports/${encodeURIComponent(f.name)}", HTML)
        self.assertIn('25 / 25 indicators reviewed', HTML)
