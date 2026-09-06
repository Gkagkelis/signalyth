from pathlib import Path


def test_home_template_uses_explicit_request_keyword():
    text = Path("app/main.py").read_text(encoding="utf-8")
    assert 'TemplateResponse(request=request, name="index.html", context={})' in text
    assert 'TemplateResponse("index.html", {"request": request})' not in text


def test_web_stack_is_bounded_for_repeatable_local_install():
    req = Path('requirements.txt').read_text(encoding='utf-8')
    assert 'fastapi==0.128.2' in req
    assert 'starlette==0.50.0' in req
    assert 'uvicorn[standard]==0.48.0' in req
