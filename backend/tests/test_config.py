"""配置迁移：固定本项目路径、UTF-8 和进程变量优先级。"""

from app.config import BACKEND_DIR, Settings
from pydantic_settings import DotEnvSettingsSource


def test_project_env_path_does_not_follow_working_directory(monkeypatch, tmp_path):
    paths = []
    def read_file(self, file_path):
        paths.append(file_path)
        return {"app_name": "本项目配置"}
    monkeypatch.setattr(DotEnvSettingsSource, "_read_env_file", read_file)
    monkeypatch.setattr("pathlib.Path.is_file", lambda self: True)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("APP_NAME=其他目录", encoding="utf-8")
    monkeypatch.delenv("APP_NAME", raising=False)
    assert Settings().app_name == "本项目配置"
    assert paths == [BACKEND_DIR / ".env"]


def test_utf8_env_and_precedence_without_global_environment_mutation(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("APP_NAME=旅行助手\nLLM_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "process-key")
    assert Settings(_env_file=env_file).app_name == "旅行助手"
    assert Settings(_env_file=env_file).llm_api_key == "process-key"
    assert Settings(_env_file=env_file, llm_api_key="explicit-key").llm_api_key == "explicit-key"
