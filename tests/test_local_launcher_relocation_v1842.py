from pathlib import Path


def test_launcher_does_not_activate_movable_virtualenv():
    text = Path('START-SIGNALYTH.command').read_text(encoding='utf-8')
    assert 'source .venv/bin/activate' not in text
    assert 'VENV_PY=".venv/bin/python3"' in text
    assert 'exec "$VENV_PY" -m uvicorn' in text


def test_launcher_uses_venv_python_for_pip():
    text = Path('START-SIGNALYTH.command').read_text(encoding='utf-8')
    assert '"$VENV_PY" -m pip install -q -r requirements.txt' in text
    assert 'python -m pip' not in text


def test_launcher_recreates_only_when_venv_python_is_missing_or_invalid():
    text = Path('START-SIGNALYTH.command').read_text(encoding='utf-8')
    assert 'if [ ! -x "$VENV_PY" ]' in text
    assert 'if ! "$VENV_PY" -c' in text
