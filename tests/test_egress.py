from pathlib import Path

import pytest

from context_forge import doctor
from context_forge import provider_http
from context_forge import status as status_mod
from context_forge.config import Settings, load_settings
from context_forge.gateways import PROVIDER_NAMES, egress_surface
from context_forge.provider_http import resolve_remote_url
from context_forge.queue import JobStore

ANTHROPIC_ENV = ('ANTHROPIC_API_URL', 'ANTHROPIC_BASE_URL',
                 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """No env leakage, and no read of the real ~/.claude/settings.json."""
    monkeypatch.setenv('CONTEXT_FORGE_HOME', str(tmp_path))
    for name in ANTHROPIC_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(provider_http, '_settings_env', lambda name: '')
    return tmp_path


def write_config(home: Path, **values) -> None:
    lines = [f'vault_path = "{(home / "vault").as_posix()}"']
    for key, value in values.items():
        lines.append(f'{key} = "{value}"' if isinstance(value, str) else f'{key} = {value}')
    (home / 'config.toml').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def settings(**values) -> Settings:
    return Settings.model_validate({'vault_path': 'vault', **values})


def test_unconfigured_provider_means_no_egress(isolated):
    kind, destination = egress_surface(None)

    assert (kind, destination) == ('none', 'nothing leaves this machine')
    ok, detail = doctor.check_egress()[1:]
    assert ok is True and 'never leave this machine' in detail


def test_recognised_providers_are_exactly_the_vocabulary():
    assert PROVIDER_NAMES == ('none', 'offline', 'fake', 'local', 'remote')


def test_local_provider_keeps_text_on_the_machine(isolated):
    write_config(isolated, model_provider='local')

    kind, destination = egress_surface(load_settings())
    ok, detail = doctor.check_egress()[1:]

    assert kind == 'local' and 'localhost' in destination
    assert ok is True and 'nothing leaves the machine' in detail


def test_remote_reports_the_endpoint_text_is_sent_to(isolated):
    write_config(isolated, model_provider='remote', remote_api_url='https://proxy.example/v1')

    assert egress_surface(load_settings())[0] == 'remote'
    ok, detail = doctor.check_egress()[1:]

    assert ok is True
    assert 'IS SENT OUT' in detail and 'https://proxy.example/v1' in detail


def test_environment_overrides_the_default_endpoint(isolated, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://from-env.example')
    write_config(isolated, model_provider='remote')

    assert resolve_remote_url(load_settings()) == 'https://from-env.example'


def test_settings_value_wins_over_environment(isolated, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://from-env.example')

    assert resolve_remote_url(settings(remote_api_url='https://from-config.example')) == (
        'https://from-config.example')


def test_default_endpoint_is_named_when_nothing_is_configured(isolated):
    assert resolve_remote_url(None) == 'https://api.anthropic.com'


def test_a_typoed_provider_fails_the_check_instead_of_passing_silently(isolated):
    write_config(isolated, model_provider='remot')

    name, ok, detail = doctor.check_egress()

    assert name == 'egress'
    assert ok is False and 'not recognised' in detail


def test_build_remote_still_honours_an_explicit_url():
    from context_forge.provider_http import build_remote

    gateway = build_remote(api_key='test-key', api_url='http://explicit.example')

    assert getattr(gateway, 'api_url') == 'http://explicit.example/v1/messages'


def test_status_json_carries_the_egress_destination(isolated):
    write_config(isolated, model_provider='remote', remote_api_url='https://proxy.example/v1')

    snapshot = status_mod.collect(JobStore(isolated / 'queue.db'))

    assert snapshot['egress'] == 'https://proxy.example/v1'


def test_egress_and_scheduled_are_wired_into_doctor(isolated, monkeypatch):
    """run_all() must keep reporting both; dropping a row from the list is silent."""
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: isolated))
    write_config(isolated, model_provider='remote', remote_api_url='https://proxy.example/v1')

    rows = {name for name, _ok, _detail in doctor.run_all()}

    assert {'egress', 'scheduled', 'host-silence'} <= rows
