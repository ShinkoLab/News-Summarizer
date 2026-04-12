import yaml
from pathlib import Path

def load_config(config_path: str = "config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        # Fallback to default structure or raise error
        raise FileNotFoundError(f"Configuration file {config_path} not found.")
    
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

# デフォルト設定をロード
config = load_config()
