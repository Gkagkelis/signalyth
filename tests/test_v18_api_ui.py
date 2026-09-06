from __future__ import annotations
import json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest

from app import main as M
from app import registry as R
from app.services import integrations as I


def _seed_registry(tmp_path):
    reg=tmp_path/'registry.json'; hist=tmp_path/'history.json'; candidates=tmp_path/'candidates.json'; secrets=tmp_path/'secrets.env'
    base={"x":{"label":"X","actor_id":"old/actor","default_actor_id":"old/actor","locked":False,"enabled":True,
        "price_per_1000_hint":1.0,"date_support":"native_exact","market_support":"query","adapter_mode":"legacy",
        "actor_status":"legacy_default_unverified","last_verified_at":None,"last_smoke_test_at":None,"actor_metadata":{},
        "input_mapping":{},"input_schema_fields":{},"input_template":{},"output_mapping":{},"mapping_confidence":None,"mapping_signature":None}}
    reg.write_text(json.dumps(base))
    return reg,hist,candidates,secrets


@pytest.fixture
def isolated(monkeypatch,tmp_path):
    reg,hist,candidates,secrets=_seed_registry(tmp_path)
    monkeypatch.setattr(R,'REGISTRY_PATH',reg); monkeypatch.setattr(R,'HISTORY_PATH',hist)
    monkeypatch.setattr(I,'CANDIDATES_FILE',candidates); monkeypatch.setattr(I,'SECRETS_FILE',secrets)
    old=(I.settings.apify_token,I.settings.openai_api_key,I.settings.signalyth_ai_enabled,I.settings.signalyth_dry_run)
    I.settings.apify_token=''; I.settings.openai_api_key=''; I.settings.signalyth_ai_enabled=False; I.settings.signalyth_dry_run=True
    try:
        yield TestClient(M.app), candidates
    finally:
        I.settings.apify_token,I.settings.openai_api_key,I.settings.signalyth_ai_enabled,I.settings.signalyth_dry_run=old


def test_credentials_are_localhost_only_and_never_echoed(isolated):
    client,_=isolated
    r=client.post('/api/integrations/credentials',json={'apify_token':'apify_123456789012345'})
    assert r.status_code==200 and r.json()['apify']['configured'] is True
    assert 'apify_123456789012345' not in r.text
    r=client.post('/api/integrations/credentials',headers={'Origin':'https://evil.example','Host':'testserver'},json={'openai_api_key':'sk-test-abcdefghijklmnopqrstuvwxyz'})
    assert r.status_code==403


def test_live_collection_toggle_is_explicit_and_persisted(isolated):
    client,_=isolated
    r=client.post('/api/integrations/credentials',json={'live_collection_enabled':True})
    assert r.status_code==200
    assert r.json()['apify']['live_collection_enabled'] is True
    assert client.get('/api/health').json()['dry_run'] is False
    r=client.post('/api/integrations/credentials',json={'live_collection_enabled':False})
    assert r.status_code==200 and r.json()['apify']['live_collection_enabled'] is False
    assert client.get('/api/health').json()['dry_run'] is True


def test_openai_paid_smoke_requires_explicit_confirm(isolated):
    client,_=isolated
    r=client.post('/api/integrations/openai/smoke')
    assert r.status_code==422 and 'confirm=true' in r.json()['detail']


def test_actor_lookup_free_probe_paid_probe_mapping_commit_flow(isolated,monkeypatch):
    client,_=isolated
    lookup={"ok":True,"actor":{"actor_id":"owner/new","title":"New","owner":"owner","is_public":True,"is_deprecated":False},
        "input_schema":{"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer"}}},
        "input_summary":{"field_count":2,"required":[],"fields":[{"name":"query","type":"string","required":False},{"name":"limit","type":"integer","required":False}]},
        "input_mapping_suggestion":{"query":"query","max_items":"limit"},"example_input":{},"looked_up_at":"2026-09-04T10:00:00+00:00"}
    monkeypatch.setattr(M,'lookup_actor',lambda ref:lookup)
    validation_calls=[]; smoke_calls=[]
    monkeypatch.setattr(M,'validate_actor_input',lambda actor_id,inp:(validation_calls.append((actor_id,inp)) or {'ok':True,'validated_at':'2026-09-04T10:01:00+00:00'}))
    def smoke(actor_id,inp,max_items,max_charge_usd):
        smoke_calls.append((actor_id,inp,max_items,max_charge_usd))
        return {'ok':True,'actor_id':actor_id,'run_id':'r1','run_status':'SUCCEEDED','dataset_id':'d1','sample_count':1,
            'sample_items':[{'content':'hello','publishedAt':'2026-09-04T10:00:00Z','permalink':'https://e/1'}],
            'output_mapping':{'mapping':{'text':'content','date':'publishedAt','url':'permalink'},'confidence':.8,
                'available_paths':['content','publishedAt','permalink'],'warnings':[]},'tested_at':'2026-09-04T10:02:00+00:00'}
    monkeypatch.setattr(M,'smoke_test_actor',smoke)

    r=client.post('/api/sources/x/actor/lookup',json={'actor_ref':'owner/new'})
    assert r.status_code==200 and smoke_calls==[]
    r=client.post('/api/sources/x/actor/probe',json={'query':'Vodafone','max_items':3,'run_paid_smoke_test':False})
    assert r.status_code==200 and validation_calls and smoke_calls==[]
    r=client.post('/api/sources/x/actor/commit')
    assert r.status_code==409  # live sample is a hard gate
    r=client.post('/api/sources/x/actor/probe',json={'query':'Vodafone','max_items':3,'max_charge_usd':0.1,'run_paid_smoke_test':True})
    assert r.status_code==200 and len(smoke_calls)==1
    r=client.patch('/api/sources/x/actor/mapping',json={'output_mapping':{'text':'content','date':'publishedAt','url':'permalink'}})
    assert r.status_code==200
    r=client.post('/api/sources/x/actor/commit')
    assert r.status_code==200
    data=r.json(); assert data['actor_id']=='owner/new' and data['locked'] is True and data['actor_status']=='verified'


def test_mapping_rejects_path_not_seen_in_real_sample(isolated,monkeypatch):
    client,_=isolated
    I.save_actor_candidate('x',{'actor':{'actor_id':'owner/new'},'input_schema':{},'input_summary':{},'input_mapping_suggestion':{'query':'q'},'example_input':{},'looked_up_at':'x'})
    I.update_actor_candidate('x',{'available_output_paths':['text','date','url']})
    r=client.patch('/api/sources/x/actor/mapping',json={'output_mapping':{'text':'made.up'}})
    assert r.status_code==422


def test_ui_contract_has_live_connection_manager_and_no_secret_storage():
    text=Path('app/templates/index.html').read_text(encoding='utf-8')
    assert '<title>SIGNALYTH v1.8.4</title>' in text
    for phrase in ['Actor Connection Manager','Find Actor','Run 3-item smoke test','Set Default & Lock','Rollback previous','Settings → Connections','Apify API token','OpenAI API key','Live collection enabled','openaiBulkModel','openaiReasoningModel','saveOpenAIModels']:
        assert phrase in text
    assert "localStorage.setItem('apify" not in text
    assert "localStorage.setItem('openai" not in text
    assert "type=\"password\"" in text
    assert 'Preview only' in text
