import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.run_manager import RunManager, RunStateError
from app.config import settings
from app.services.storage import RunStore


TERMINAL = {"cancelled", "succeeded", "completed_shortfall", "completed_with_errors", "failed", "interrupted"}


def wait_terminal(store: RunStore, run_id: str, timeout=3.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = store.read_status(run_id)
        if last.get("status") in TERMINAL:
            return last
        time.sleep(0.01)
    raise AssertionError(f"run did not reach terminal state: {last}")


def plan_two_sources():
    return {
        "client": "C", "topic": "T", "market": "Greece",
        "max_budget_usd": 2.0, "date_from": "2026-08-01", "date_to": "2026-08-31",
        "sample_mode": "perSource", "target_total": 2, "rebalancing_enabled": False,
        "sources": [
            {"source": "x", "target_items": 1, "price_per_1000_hint": 0.15,
             "subruns": [{"actor_id": "fake/x", "input": {"maxItems": 1}, "target_items": 1, "max_charge_usd": 0.2}]},
            {"source": "news", "target_items": 1, "price_per_1000_hint": 1.0,
             "subruns": [{"actor_id": "fake/news", "input": {"maxItems": 1}, "target_items": 1, "max_charge_usd": 0.2}]},
        ],
    }


class FastRunner:
    calls = []

    def __init__(self):
        pass

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.__class__.calls.append(actor_id)
        return {"id": actor_id, "defaultDatasetId": "d", "usageTotalUsd": min(0.01, max_charge_usd)}, [
            {"text": actor_id, "timestamp": "2026-08-15T10:00:00Z", "url": f"https://example/{actor_id}"}
        ]


class BlockingRunner:
    calls = []
    started = threading.Event()
    release = threading.Event()

    def __init__(self):
        pass

    def run(self, actor_id, run_input, *, max_items, max_charge_usd):
        self.__class__.calls.append(actor_id)
        self.__class__.started.set()
        self.__class__.release.wait(timeout=2)
        return {"id": actor_id, "defaultDatasetId": "d", "usageTotalUsd": min(0.01, max_charge_usd)}, [
            {"text": actor_id, "timestamp": "2026-08-15T10:00:00Z", "url": f"https://example/{actor_id}"}
        ]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore()
        self.store.root = Path(self.tmp.name)
        self.manager = RunManager(store=self.store, max_workers=1)
        FastRunner.calls = []
        BlockingRunner.calls = []
        BlockingRunner.started = threading.Event()
        BlockingRunner.release = threading.Event()
        self.old_ai_enabled = settings.signalyth_ai_enabled
        self.old_openai_key = settings.openai_api_key
        settings.signalyth_ai_enabled = False
        settings.openai_api_key = ""

    def tearDown(self):
        settings.signalyth_ai_enabled = self.old_ai_enabled
        settings.openai_api_key = self.old_openai_key
        BlockingRunner.release.set()
        self.manager.shutdown(wait=True)
        self.tmp.cleanup()

    def make_run(self, plan=None):
        return self.store.create(plan or plan_two_sources())[0]

    def test_background_run_exposes_progress_and_finishes(self):
        run_id = self.make_run()
        with patch("app.services.collector.ApifyRunner", FastRunner):
            first = self.manager.enqueue(run_id)
            self.assertIn(first['status'], {'queued', 'running', 'succeeded'})
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'succeeded')
        self.assertEqual(done['progress']['percent'], 100)
        self.assertEqual(done['progress']['completed_sources'], 2)
        self.assertEqual(done['normalized_total'], 2)
        self.assertEqual(done['sources']['x']['status'], 'succeeded')
        self.assertEqual(done['sources']['news']['status'], 'succeeded')
        self.assertEqual(done['sources']['x']['subruns_completed'], 1)
        self.assertEqual(done['current']['code'], 'cleaning_completed')
        self.assertEqual(done['cleaning']['status'], 'succeeded')
        self.assertTrue((self.store.folder_for(run_id) / 'cleaning' / 'report.json').exists())

    def test_double_start_is_idempotent_while_active(self):
        run_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        with patch("app.services.collector.ApifyRunner", BlockingRunner):
            self.manager.enqueue(run_id)
            self.assertTrue(BlockingRunner.started.wait(timeout=1))
            second = self.manager.enqueue(run_id)
            self.assertIn(second['status'], {'running', 'queued'})
            self.assertEqual(len(BlockingRunner.calls), 1)
            BlockingRunner.release.set()
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'succeeded')
        self.assertEqual(len(BlockingRunner.calls), 1)
        with self.assertRaises(RunStateError):
            self.manager.enqueue(run_id)

    def test_cancel_running_stops_before_next_actor_and_preserves_partial_data(self):
        run_id = self.make_run()
        with patch("app.services.collector.ApifyRunner", BlockingRunner):
            self.manager.enqueue(run_id)
            self.assertTrue(BlockingRunner.started.wait(timeout=1))
            cancelling = self.manager.cancel(run_id)
            self.assertIn(cancelling['status'], {'cancelling', 'cancelled'})
            BlockingRunner.release.set()
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'cancelled')
        self.assertEqual(len(BlockingRunner.calls), 1)
        self.assertEqual(done['sources']['x']['status'], 'cancelled_partial')
        self.assertEqual(done['sources']['news']['status'], 'skipped_cancelled')
        self.assertEqual(done['normalized_total'], 1)
        self.assertTrue((self.store.folder_for(run_id) / 'normalized-all.json').exists())
        self.assertEqual(done['current']['code'], 'cancel_completed')


    def test_cancel_queued_run_never_starts_its_actor(self):
        first_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        second_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][1]], "target_total": 1})
        with patch("app.services.collector.ApifyRunner", BlockingRunner):
            self.manager.enqueue(first_id)
            self.assertTrue(BlockingRunner.started.wait(timeout=1))
            queued = self.manager.enqueue(second_id)
            self.assertEqual(queued['status'], 'queued')
            cancelled = self.manager.cancel(second_id)
            self.assertEqual(cancelled['status'], 'cancelled')
            BlockingRunner.release.set()
            wait_terminal(self.store, first_id)
        self.assertEqual(BlockingRunner.calls, ['fake/x'])
        self.assertEqual(self.store.read_status(second_id)['sources']['news']['status'], 'skipped_cancelled')

    def test_cancel_planned_never_calls_runner(self):
        run_id = self.make_run()
        status = self.manager.cancel(run_id)
        self.assertEqual(status['status'], 'cancelled')
        self.assertEqual(status['progress']['percent'], 100)
        self.assertTrue(all(v['status'] == 'skipped_cancelled' for v in status['sources'].values()))

    def test_unexpected_worker_error_becomes_failed_not_infinite_spinner(self):
        run_id = self.make_run()
        with patch("app.services.run_manager.execute_plan", side_effect=RuntimeError("boom")):
            self.manager.enqueue(run_id)
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'failed')
        self.assertEqual(done['progress']['percent'], 100)
        self.assertIn('boom', done['fatal_error'])
        self.assertEqual(done['current']['code'], 'run_failed')

    def test_cleaning_failure_after_collection_becomes_failed_safely(self):
        run_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        with patch("app.services.collector.ApifyRunner", FastRunner), patch("app.services.run_manager.clean_run", side_effect=RuntimeError("cleaner boom")):
            self.manager.enqueue(run_id)
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'failed')
        self.assertIn('cleaner boom', done['fatal_error'])
        self.assertTrue((self.store.folder_for(run_id) / 'normalized-all.json').exists())

    def test_pipeline_does_not_expose_collection_terminal_before_cleaning(self):
        run_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        gate = threading.Event()
        def slow_clean(*args, **kwargs):
            gate.wait(timeout=1)
            return {"ruleset_version":"0.8.0", "data_quality_score":80, "trusted_records":0, "review_records":0, "excluded_records":1}
        with patch("app.services.collector.ApifyRunner", FastRunner), patch("app.services.run_manager.clean_run", side_effect=slow_clean):
            self.manager.enqueue(run_id)
            deadline=time.time()+1
            seen_cleaning=False
            while time.time()<deadline:
                st=self.store.read_status(run_id)
                if st.get('phase')=='cleaning':
                    seen_cleaning=True
                    self.assertEqual(st.get('status'),'running')
                    break
                time.sleep(.01)
            self.assertTrue(seen_cleaning)
            gate.set()
            done=wait_terminal(self.store,run_id)
        self.assertEqual(done['current']['code'],'cleaning_completed')

    def test_cleaning_failure_marks_cleaning_substatus_failed(self):
        run_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        with patch("app.services.collector.ApifyRunner", FastRunner), patch("app.services.run_manager.clean_run", side_effect=RuntimeError("cleaner exploded")):
            self.manager.enqueue(run_id)
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'failed')
        self.assertEqual(done['cleaning']['status'], 'failed')
        self.assertIn('cleaner exploded', done['cleaning']['error'])

    def test_restart_during_cleaning_marks_cleaning_interrupted(self):
        run_id = self.make_run()
        status = self.store.read_status(run_id)
        status.update({"status": "running", "phase": "cleaning"})
        status['cleaning'].update({"status":"running", "started_at":"2026-09-03T10:00:00+00:00"})
        self.store.write_status(run_id, status)
        self.store.recover_interrupted_runs()
        after = self.store.read_status(run_id)
        self.assertEqual(after['status'], 'interrupted')
        self.assertEqual(after['cleaning']['status'], 'interrupted')
        self.assertIn('restart', after['cleaning']['error'].lower())

    def test_process_restart_recovery_marks_active_runs_interrupted(self):
        run_id = self.make_run()
        status = self.store.read_status(run_id)
        status.update({"status": "running", "phase": "collecting"})
        self.store.write_status(run_id, status)
        recovered = self.store.recover_interrupted_runs()
        self.assertIn(run_id, recovered)
        after = self.store.read_status(run_id)
        self.assertEqual(after['status'], 'interrupted')
        self.assertEqual(after['progress']['percent'], 100)
        self.assertIn('restarted', after['fatal_error'])
        self.assertEqual(after['progress']['completed_sources'], after['progress']['total_sources'])

    def test_live_pipeline_can_continue_from_cleaning_into_ai_without_exposing_false_completion(self):
        run_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        settings.signalyth_ai_enabled = True
        settings.openai_api_key = "fake"
        clean_report = {"ruleset_version":"0.8.0", "data_quality_score":90, "trusted_records":1, "review_records":0, "excluded_records":0}
        ai_report = {"ruleset_version":"0.9.0", "prompt_version":"signalyth-semantic-v0.9", "generated_at":"2026-09-03T11:00:00+00:00", "analysis_ready_records":1, "review_records":0}
        intelligence_report = {"ruleset_version":"1.0.0", "methodology_version":"signalyth-intelligence-v1", "generated_at":"2026-09-03T11:01:00+00:00", "brand_reputation":{"index":60.0}}
        investigation_report = {"ruleset_version":"1.1.0", "methodology_version":"signalyth-investigations-v1", "evidence_contract_version":"signalyth-evidence-pack-v1.1", "generated_at":"2026-09-03T11:02:00+00:00", "investigation_count":1, "indicator_contract":{"required":25,"examined":25,"available":10,"omitted":0,"complete":True}}
        with patch("app.services.collector.ApifyRunner", FastRunner), patch("app.services.run_manager.clean_run", return_value=clean_report), patch("app.services.run_manager.analyze_run", return_value=ai_report) as ai_call, patch("app.services.run_manager.build_intelligence", return_value=intelligence_report) as intel_call, patch("app.services.run_manager.build_investigations", return_value=investigation_report) as inv_call, patch("app.services.run_manager.build_visualizations", return_value={"ruleset_version":"1.2.0", "methodology_version":"signalyth-visual-intelligence-v1", "visual_contract_version":"signalyth-visual-pack-v1.2", "generated_at":"2026-09-03T11:03:00+00:00", "chart_count":8, "indicator_contract":{"required":25,"examined":25,"complete":True}}) as viz_call:
            self.manager.enqueue(run_id)
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'succeeded')
        self.assertEqual(done['phase'], 'visualizations_ready')
        self.assertEqual(done['cleaning']['status'], 'succeeded')
        self.assertEqual(done['analysis']['status'], 'succeeded')
        self.assertEqual(done['current']['code'], 'visualizations_completed')
        ai_call.assert_called_once()
        intel_call.assert_called_once()
        inv_call.assert_called_once()
        viz_call.assert_called_once()

    def test_ai_failure_preserves_collection_and_cleaning_evidence_and_fails_safely(self):
        run_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        settings.signalyth_ai_enabled = True
        settings.openai_api_key = "fake"
        clean_report = {"ruleset_version":"0.8.0", "data_quality_score":90, "trusted_records":1, "review_records":0, "excluded_records":0}
        with patch("app.services.collector.ApifyRunner", FastRunner), patch("app.services.run_manager.clean_run", return_value=clean_report), patch("app.services.run_manager.analyze_run", side_effect=RuntimeError("model unavailable")):
            self.manager.enqueue(run_id)
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'failed')
        self.assertEqual(done['phase'], 'ai_analysis_failed')
        self.assertEqual(done['cleaning']['status'], 'succeeded')
        self.assertEqual(done['analysis']['status'], 'failed')
        self.assertIn('model unavailable', done['fatal_error'])
        self.assertTrue((self.store.folder_for(run_id) / 'normalized-all.json').exists())

    def test_restart_during_ai_marks_ai_interrupted(self):
        run_id = self.make_run()
        status = self.store.read_status(run_id)
        status.update({"status": "running", "phase": "ai_analysis"})
        status['analysis'].update({"status":"running", "started_at":"2026-09-03T10:00:00+00:00"})
        self.store.write_status(run_id, status)
        self.store.recover_interrupted_runs()
        after = self.store.read_status(run_id)
        self.assertEqual(after['status'], 'interrupted')
        self.assertEqual(after['analysis']['status'], 'interrupted')
        self.assertIn('restart', after['analysis']['error'].lower())

    def test_investigation_failure_preserves_intelligence_evidence_and_fails_safely(self):
        run_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        settings.signalyth_ai_enabled = True
        settings.openai_api_key = "fake"
        clean_report = {"ruleset_version":"0.8.0", "data_quality_score":90, "trusted_records":1, "review_records":0, "excluded_records":0}
        ai_report = {"ruleset_version":"0.9.0", "prompt_version":"signalyth-semantic-v0.9", "generated_at":"2026-09-03T11:00:00+00:00", "analysis_ready_records":1, "review_records":0}
        intelligence_report = {"ruleset_version":"1.0.0", "methodology_version":"signalyth-intelligence-v1", "generated_at":"2026-09-03T11:01:00+00:00", "brand_reputation":{"index":60.0}}
        with patch("app.services.collector.ApifyRunner", FastRunner), patch("app.services.run_manager.clean_run", return_value=clean_report), patch("app.services.run_manager.analyze_run", return_value=ai_report), patch("app.services.run_manager.build_intelligence", return_value=intelligence_report), patch("app.services.run_manager.build_investigations", side_effect=RuntimeError("investigator boom")):
            self.manager.enqueue(run_id)
            done = wait_terminal(self.store, run_id)
        self.assertEqual(done['status'], 'failed')
        self.assertEqual(done['phase'], 'investigations_failed')
        self.assertEqual(done['intelligence']['status'], 'succeeded')
        self.assertEqual(done['investigations']['status'], 'failed')
        self.assertIn('investigator boom', done['fatal_error'])

    def test_pipeline_does_not_expose_terminal_before_investigations(self):
        run_id = self.make_run({**plan_two_sources(), "sources": [plan_two_sources()['sources'][0]], "target_total": 1})
        settings.signalyth_ai_enabled = True
        settings.openai_api_key = "fake"
        clean_report = {"ruleset_version":"0.8.0", "data_quality_score":90, "trusted_records":1, "review_records":0, "excluded_records":0}
        ai_report = {"ruleset_version":"0.9.0", "prompt_version":"signalyth-semantic-v0.9", "generated_at":"2026-09-03T11:00:00+00:00", "analysis_ready_records":1, "review_records":0}
        intelligence_report = {"ruleset_version":"1.0.0", "methodology_version":"signalyth-intelligence-v1", "generated_at":"2026-09-03T11:01:00+00:00", "brand_reputation":{"index":60.0}}
        gate = threading.Event()
        def slow_inv(*args, **kwargs):
            gate.wait(timeout=1)
            return {"ruleset_version":"1.1.0", "methodology_version":"signalyth-investigations-v1", "evidence_contract_version":"signalyth-evidence-pack-v1.1", "generated_at":"2026-09-03T11:02:00+00:00", "investigation_count":1, "indicator_contract":{"required":25,"examined":25,"available":10,"omitted":0,"complete":True}}
        with patch("app.services.collector.ApifyRunner", FastRunner), patch("app.services.run_manager.clean_run", return_value=clean_report), patch("app.services.run_manager.analyze_run", return_value=ai_report), patch("app.services.run_manager.build_intelligence", return_value=intelligence_report), patch("app.services.run_manager.build_investigations", side_effect=slow_inv), patch("app.services.run_manager.build_visualizations", return_value={"ruleset_version":"1.2.0", "methodology_version":"signalyth-visual-intelligence-v1", "visual_contract_version":"signalyth-visual-pack-v1.2", "generated_at":"2026-09-03T11:03:00+00:00", "chart_count":8, "indicator_contract":{"required":25,"examined":25,"complete":True}}):
            self.manager.enqueue(run_id)
            deadline=time.time()+1
            seen=False
            while time.time()<deadline:
                st=self.store.read_status(run_id)
                if st.get('phase')=='investigations':
                    seen=True
                    self.assertEqual(st.get('status'),'running')
                    self.assertEqual(st.get('progress',{}).get('percent'),98)
                    break
                time.sleep(.01)
            self.assertTrue(seen)
            gate.set()
            done=wait_terminal(self.store,run_id)
        self.assertEqual(done['phase'],'visualizations_ready')
        self.assertEqual(done['current']['code'],'visualizations_completed')

    def test_restart_during_investigations_marks_investigations_interrupted(self):
        run_id = self.make_run()
        status = self.store.read_status(run_id)
        status.update({"status":"running", "phase":"investigations"})
        status['investigations'].update({"status":"running", "started_at":"2026-09-03T10:00:00+00:00"})
        self.store.write_status(run_id,status)
        self.store.recover_interrupted_runs()
        after=self.store.read_status(run_id)
        self.assertEqual(after['status'],'interrupted')
        self.assertEqual(after['investigations']['status'],'interrupted')
        self.assertIn('restart', after['investigations']['error'].lower())


    def test_atomic_status_write_survives_aggressive_polling(self):
        run_id = self.make_run()
        errors = []

        def writer():
            try:
                for i in range(300):
                    s = self.store.read_status(run_id)
                    s['test_counter'] = i
                    self.store.write_status(run_id, s)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(600):
                s = self.store.read_status(run_id)
                self.assertIsInstance(s, dict)
                self.assertEqual(s['run_id'], run_id)
        finally:
            thread.join(timeout=2)
        self.assertFalse(errors)


if __name__ == '__main__':
    unittest.main()

class Step7LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=RunStore(); self.store.root=Path(self.tmp.name); self.manager=RunManager(store=self.store,max_workers=1)
        self.old_ai_enabled=settings.signalyth_ai_enabled; self.old_openai_key=settings.openai_api_key
        settings.signalyth_ai_enabled=True; settings.openai_api_key='fake'
    def tearDown(self):
        settings.signalyth_ai_enabled=self.old_ai_enabled; settings.openai_api_key=self.old_openai_key; self.manager.shutdown(wait=True); self.tmp.cleanup()
    def make_run(self):
        p=plan_two_sources(); p={**p,'sources':[p['sources'][0]],'target_total':1}; return self.store.create(p)[0]
    def reports(self):
        return (
            {"ruleset_version":"0.8.0","data_quality_score":90,"trusted_records":1,"review_records":0,"excluded_records":0},
            {"ruleset_version":"0.9.0","prompt_version":"signalyth-semantic-v0.9","generated_at":"2026-09-03T11:00:00+00:00","analysis_ready_records":1,"review_records":0},
            {"ruleset_version":"1.0.0","methodology_version":"signalyth-intelligence-v1","generated_at":"2026-09-03T11:01:00+00:00","brand_reputation":{"index":60.0}},
            {"ruleset_version":"1.1.0","methodology_version":"signalyth-investigations-v1","evidence_contract_version":"signalyth-evidence-pack-v1.1","generated_at":"2026-09-03T11:02:00+00:00","investigation_count":1,"indicator_contract":{"required":25,"examined":25,"complete":True}},
            {"ruleset_version":"1.2.0","methodology_version":"signalyth-visual-intelligence-v1","visual_contract_version":"signalyth-visual-pack-v1.2","generated_at":"2026-09-03T11:03:00+00:00","chart_count":8,"indicator_contract":{"required":25,"examined":25,"complete":True}},
        )

    def test_visualization_failure_preserves_step6_evidence_and_fails_safely(self):
        run_id=self.make_run(); clean,ai,intel,inv,_=self.reports()
        with patch('app.services.collector.ApifyRunner',FastRunner), patch('app.services.run_manager.clean_run',return_value=clean), patch('app.services.run_manager.analyze_run',return_value=ai), patch('app.services.run_manager.build_intelligence',return_value=intel), patch('app.services.run_manager.build_investigations',return_value=inv), patch('app.services.run_manager.build_visualizations',side_effect=RuntimeError('visual boom')):
            self.manager.enqueue(run_id); done=wait_terminal(self.store,run_id)
        self.assertEqual(done['status'],'failed'); self.assertEqual(done['phase'],'visualizations_failed')
        self.assertEqual(done['investigations']['status'],'succeeded'); self.assertEqual(done['visualizations']['status'],'failed')
        self.assertIn('visual boom',done['fatal_error'])

    def test_pipeline_stays_running_until_visualizations_finish(self):
        run_id=self.make_run(); clean,ai,intel,inv,viz=self.reports(); gate=threading.Event(); entered=threading.Event()
        def slow_viz(*args,**kwargs): entered.set(); gate.wait(timeout=1); return viz
        with patch('app.services.collector.ApifyRunner',FastRunner), patch('app.services.run_manager.clean_run',return_value=clean), patch('app.services.run_manager.analyze_run',return_value=ai), patch('app.services.run_manager.build_intelligence',return_value=intel), patch('app.services.run_manager.build_investigations',return_value=inv), patch('app.services.run_manager.build_visualizations',side_effect=slow_viz):
            self.manager.enqueue(run_id); self.assertTrue(entered.wait(timeout=1)); st=self.store.read_status(run_id)
            self.assertEqual(st['status'],'running'); self.assertEqual(st['phase'],'visualizations'); self.assertEqual(st['progress']['percent'],99)
            gate.set(); done=wait_terminal(self.store,run_id)
        self.assertEqual(done['status'],'succeeded'); self.assertEqual(done['phase'],'visualizations_ready'); self.assertEqual(done['current']['code'],'visualizations_completed')

    def test_cancel_during_visualizations_preserves_prior_layers(self):
        from app.services.visualizations import VisualizationCancelled
        run_id=self.make_run(); clean,ai,intel,inv,_=self.reports(); entered=threading.Event()
        def cancellable(*args,cancel_check=None,**kwargs):
            entered.set(); deadline=time.time()+2
            while time.time()<deadline:
                if cancel_check and cancel_check(): raise VisualizationCancelled('cancelled')
                time.sleep(.01)
            raise AssertionError('cancel was not observed')
        with patch('app.services.collector.ApifyRunner',FastRunner), patch('app.services.run_manager.clean_run',return_value=clean), patch('app.services.run_manager.analyze_run',return_value=ai), patch('app.services.run_manager.build_intelligence',return_value=intel), patch('app.services.run_manager.build_investigations',return_value=inv), patch('app.services.run_manager.build_visualizations',side_effect=cancellable):
            self.manager.enqueue(run_id); self.assertTrue(entered.wait(timeout=1)); self.manager.cancel(run_id); done=wait_terminal(self.store,run_id)
        self.assertEqual(done['status'],'cancelled'); self.assertEqual(done['investigations']['status'],'succeeded'); self.assertEqual(done['visualizations']['status'],'cancelled')

    def test_restart_during_visualizations_marks_visual_layer_interrupted(self):
        run_id=self.make_run(); st=self.store.read_status(run_id); st.update({'status':'running','phase':'visualizations'}); st['visualizations'].update({'status':'running','started_at':'2026-09-03T10:00:00+00:00'}); self.store.write_status(run_id,st)
        self.store.recover_interrupted_runs(); after=self.store.read_status(run_id)
        self.assertEqual(after['status'],'interrupted'); self.assertEqual(after['visualizations']['status'],'interrupted'); self.assertIn('restart',after['visualizations']['error'].lower())
