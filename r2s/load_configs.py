import os
import yaml
import logging.config
import logging
import coloredlogs

def setup_logging(default_path='../config/logging.yml', default_level=logging.INFO, env_key='LOG_CFG'):
    """
    | **@author:** Prathyush SP
    | Logging Setup
    """
    path = default_path
    value = os.getenv(env_key, None)
    if value:
        path = value
    if os.path.exists(path):
        with open(path, 'rt') as f:
            try:
                config = yaml.safe_load(f.read())
                logging.config.dictConfig(config)
                # Change field style of levelname
                custom_field_styles = coloredlogs.DEFAULT_FIELD_STYLES
                custom_field_styles['levelname']['color']='yellow'
                coloredlogs.install(field_styles=custom_field_styles)
            except Exception as e:
                print(e)
                print('Error in Logging Configuration. Using default configs')
                logging.basicConfig(level=default_level)
                coloredlogs.install(level=default_level)
    else:
        logging.basicConfig(level=default_level)
        coloredlogs.install(level=default_level)
        print('Failed to load configuration file. Using default configs')

def load_config():
    """ Load config data from config.yml """

    with open("../config/config.yml", 'r') as stream:
        try:
            return yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            print(exc)
