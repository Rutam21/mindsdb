import os
import json
import argparse
import datetime
from copy import deepcopy
from pathlib import Path

from appdirs import user_data_dir

# NOTE do not `import from mindsdb` here


def _merge_key_recursive(target_dict, source_dict, key):
    if key not in target_dict:
        target_dict[key] = source_dict[key]
    elif not isinstance(target_dict[key], dict) or not isinstance(source_dict[key], dict):
        target_dict[key] = source_dict[key]
    else:
        for k in list(source_dict[key].keys()):
            _merge_key_recursive(target_dict[key], source_dict[key], k)


def _merge_configs(original_config: dict, override_config: dict) -> dict:
    for key in list(override_config.keys()):
        _merge_key_recursive(original_config, override_config, key)
    return original_config


def get_or_create_data_dir():
    data_dir = user_data_dir("mindsdb", "mindsdb")
    mindsdb_data_dir = os.path.join(data_dir, "var/")

    if os.path.exists(mindsdb_data_dir) is False:
        Path(mindsdb_data_dir).mkdir(mode=0o777, exist_ok=True, parents=True)

    try:
        assert os.path.exists(mindsdb_data_dir)
        assert os.access(mindsdb_data_dir, os.W_OK) is True
    except Exception:
        raise Exception(
            "MindsDB storage directory does not exist and could not be created"
        )

    return mindsdb_data_dir


class Config:
    """
    Attributes:
        __instance (Config): instance of 'Config' to make it singleton
        _config (dict): application config, the result of merging other configs
        _user_config (dict): config provided by the user (usually with cmd arg `--config=config.json`)
        _env_config (dict): config collected from different env vars
        _auto_config (dict): config that is editd by the app itself (e.g. when you change values in GUI)
        _default_config (dict): config with default values
        config_path (Path): path to the `config.json` provided by the user
        storage_root_path (Path): path to storage root folder
        auto_config_path (Path): path to `config.auto.json`
        auto_config_mtime (float): mtime of `config.auto.json` when it was loaded to `self._auto_config`
        _cmd_args (argparse.Namespace): cmd args
        use_docker_env (bool): is the app run in docker env
    """
    __instance: 'Config' = None

    _config: dict = None
    _user_config: dict = None
    _env_config: dict = None
    _auto_config: dict = None
    _default_config: dict = None
    config_path: Path = None
    storage_root_path: Path = None
    auto_config_path: Path = None
    auto_config_mtime: float = 0
    _cmd_args: argparse.Namespace = None
    use_docker_env: bool = os.environ.get('MINDSDB_DOCKER_ENV', False) is not False

    def __new__(cls, *args, **kwargs):
        """Make class singletone
        """
        if cls.__instance is not None:
            return cls.__instance

        self = super().__new__(cls, *args, **kwargs)
        cls.__instance = self

        self.fetch_user_config()

        # region determine root path
        if self.storage_root_path is None:
            if isinstance(os.environ.get('MINDSDB_STORAGE_DIR'), str):
                self.storage_root_path = os.environ.get['MINDSDB_STORAGE_DIR']
            elif 'root' in self._user_config.get('paths', {}):
                self.storage_root_path = self.user_config['paths']['root']
            else:
                self.storage_root_path = get_or_create_data_dir()
            self.storage_root_path = Path(self.storage_root_path)
            if self.storage_root_path.exists() is False:
                self.storage_root_path.mkdir(mode=0o777, exist_ok=True, parents=True)
        # endregion

        # region prepare default config
        api_host = "127.0.0.1" if not self.use_docker_env else "0.0.0.0"
        self._default_config = {
            'permanent_storage': {
                'location': 'absent'
            },
            "storage_db": (
                'sqlite:///'
                + str(self.storage_root_path / 'mindsdb.sqlite3.db')
                + '?check_same_thread=False&timeout=30'
            ),
            'paths': {
                'root': self.storage_root_path,
                'content': self.storage_root_path / 'content',
                'storage': self.storage_root_path / 'storage',
                'static': self.storage_root_path / 'static',
                'tmp': self.storage_root_path / 'tmp',
                'log': self.storage_root_path / 'log',
                'cache': self.storage_root_path / 'cache',
                'locks': self.storage_root_path / 'locks',
            },
            'auth': {
                'http_auth_enabled': False,
                "http_permanent_session_lifetime": datetime.timedelta(days=31)
            },
            "log": {
                "level": {
                    "console": "INFO",
                    "file": "DEBUG",
                    "db": "WARNING"
                }
            },
            "gui": {
                "autoupdate": True
            },
            "debug": False,
            "environment": "local",
            "integrations": {},
            "api": {
                "http": {
                    "host": api_host,
                    "port": "47334",
                    "restart_on_failure": True,
                    "max_restart_count": 1,
                    "max_restart_interval_seconds": 60
                },
                "mysql": {
                    "host": api_host,
                    "password": "",
                    "port": "47335",
                    "database": "mindsdb",
                    "ssl": True,
                    "restart_on_failure": True,
                    "max_restart_count": 1,
                    "max_restart_interval_seconds": 60
                },
                "mongodb": {
                    "host": api_host,
                    "port": "47336",
                    "database": "mindsdb"
                },
                "postgres": {
                    "host": api_host,
                    "port": "55432",
                    "database": "mindsdb"
                }
            },
            "cache": {
                "type": "local"
            },
            'ml_task_queue': {
                'type': 'local'
            },
            "file_upload_domains": [],
            "web_crawling_allowed_sites": [],
            "cloud": False
        }
        # endregion

        # region find 'auto' config file, create if not exists
        auto_config_name = 'config.auto.json'
        auto_config_path = self.storage_root_path.joinpath(auto_config_name)
        if not auto_config_path.is_file():
            auto_config_path.write_text('{}')
        self.auto_config_path = auto_config_path
        # endregion

        self.prepare_env_config()

        self.fetch_auto_config()
        self.merge_configs()

        return cls.__instance

    def prepare_env_config(self):
        self._env_config = {}

        # region storage root path
        if os.environ.get('MINDSDB_STORAGE_DIR', '') != '':
            self._env_config['paths'] = {
                'root': Path(os.environ.get['MINDSDB_STORAGE_DIR'])
            }
        # endregion

        # region vars: permanent storage disabled?
        if os.environ.get('MINDSDB_STORAGE_BACKUP_DISABLED', '').lower() in ('1', 'true'):
            self._env_config['permanent_storage'] = {
                'location': 'absent'
            }
        # endregion

        # region vars: ml queue
        if os.environ.get('MINDSDB_ML_QUEUE_TYPE', '').lower() == 'redis':
            self._env_config['ml_task_queue'] = {
                'type': 'redis',
                'host': os.environ.get('MINDSDB_ML_QUEUE_HOST', 'localhost'),
                'port': int(os.environ.get('MINDSDB_ML_QUEUE_PORT', 6379)),
                'db': int(os.environ.get('MINDSDB_ML_QUEUE_DB', 0)),
                'username': os.environ.get('MINDSDB_ML_QUEUE_USERNAME'),
                'password': os.environ.get('MINDSDB_ML_QUEUE_PASSWORD')
            }
        # endregion

        # region vars: username and password
        http_username = os.environ.get('MINDSDB_USERNAME')
        http_password = os.environ.get('MINDSDB_PASSWORD')

        if bool(http_username) != bool(http_password):
            raise ValueError('Both MINDSDB_USERNAME and MINDSDB_PASSWORD must be set together and must be non-empty strings.')

        # If both username and password are set, enable HTTP auth.
        if http_username and http_password:
            self._env_config['auth'] = {
                'http_auth_enabled': True,
                'username': http_username,
                'password': http_password
            }
        # endregion

        # region permanent session lifetime
        for env_name in ('MINDSDB_HTTP_PERMANENT_SESSION_LIFETIME', 'FLASK_PERMANENT_SESSION_LIFETIME'):
            env_value = os.environ.get(env_name)
            if isinstance(env_value, str):
                try:
                    permanent_session_lifetime = int(env_value)
                except Exception:
                    raise ValueError(f'Warning: Can\'t cast env var {env_name} value to int: {env_value}')
                if 'auth' not in self._env_config:
                    self._env_config['auth'] = {}
                self._env_config['auth']['http_permanent_session_lifetime'] = permanent_session_lifetime
                break
        # endregion

        if os.environ.get('MINDSDB_DB_CON', '') != '':
            self._env_config['storage_db'] = os.environ['MINDSDB_DB_CON']

    def parse_cmd_args(self):
        if self._cmd_args is not None:
            return

        parser = argparse.ArgumentParser(description='CL argument for mindsdb server')
        parser.add_argument('--api', type=str, default=None)
        parser.add_argument('--config', type=str, default=None)
        parser.add_argument('--install-handlers', type=str, default=None)
        parser.add_argument('--verbose', action='store_true')
        parser.add_argument('--no_studio', action='store_true')
        parser.add_argument('-v', '--version', action='store_true')
        parser.add_argument('--ml_task_queue_consumer', action='store_true', default=None)
        self._cmd_args, _ = parser.parse_known_args()

    def fetch_auto_config(self) -> bool:
        """Set global variable `auto_config` to dict readed from config.auto.json.
        Do it only if `user_config` was not loaded before or been changed.

        Returns:
            bool: True if config was loaded or updated, False if config was not changed.
        """

        if self.auto_config_mtime != self.auto_config_path.stat().st_mtime:
            try:
                self._auto_config = json.loads(self.auto_config_path.read_text())
            except json.JSONDecodeError as e:
                raise ValueError(f"The 'auto' configuration file ({self.auto_config_path}) contains invalid JSON: {e}")
            self.auto_config_mtime = self.auto_config_path.stat().st_mtime
            return True
        return False

    def fetch_user_config(self) -> bool:
        """Set `_user_config` to config provided by the user. Do it only if `_user_config` was not loaded before.

        Returns:
            bool: True if config was loaded, False if it was loaded before.
        """
        if self._user_config is None:
            cmd_args_config = self.cmd_args.config
            if isinstance(cmd_args_config, str):
                self.config_path = cmd_args_config
            elif isinstance(os.environ.get('MINDSDB_CONFIG_PATH'), str):
                self.config_path = os.environ['MINDSDB_CONFIG_PATH']
            if self.config_path == 'absent':
                self.config_path = None
            if isinstance(self.config_path, str):
                self.config_path = Path(self.config_path)
                if not self.config_path.is_file():
                    raise FileNotFoundError(f'The configuration file was not found at the path: {self.config_path}')
                try:
                    self._user_config = json.loads(self.config_path.read_text())
                except json.JSONDecodeError as e:
                    raise ValueError(f'The configuration file ({self.config_path}) contains invalid JSON: {e}')
            else:
                self._user_config = {}
            return True
        return False

    def merge_configs(self) -> None:
        new_config = deepcopy(self._default_config)
        _merge_configs(new_config, self._user_config)
        _merge_configs(new_config, self._auto_config)
        _merge_configs(new_config, self._env_config)

        # region create dirs
        for key, value in new_config['paths'].items():
            if isinstance(value, str):
                new_config['paths'][key] = Path(value)
            elif isinstance(value, Path) is False:
                raise ValueError(f"Unexpected path value: {value}")
            new_config['paths'][key].mkdir(mode=0o777, exist_ok=True, parents=True)
        # endregion

        self._config = new_config

    def __getitem__(self, key):
        updated = self.fetch_auto_config()
        if updated:
            self.init_config()
        return self._config[key]

    def get(self, key, default=None):
        updated = self.fetch_auto_config()
        if updated:
            self.init_config()
        return self._config.get(key, default)

    def get_all(self):
        updated = self.fetch_auto_config()
        if updated:
            self.init_config()
        return self._config

    def update(self, data: dict):
        _merge_configs(self._auto_config, data)

        self.auto_config_path.write_text(
            json.dumps(self._auto_config, indent=4)
        )

        self.auto_config_mtime = self.auto_config_path.stat().st_mtime

        self.merge_configs()

    @property
    def cmd_args(self):
        if self._cmd_args is None:
            self.parse_cmd_args()
        return self._cmd_args

    @property
    def paths(self):
        return self._config['paths']

    @property
    def user_config(self):
        return self._user_config

    @property
    def auto_config(self):
        return self._auto_config

    @property
    def env_config(self):
        return self._env_config


config = Config()
