from os import environ

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from commons.models import *

db_url = environ.get('DB')
if db_url is None or db_url == '':
    raise Exception('Please specify connection URL by setting the DB environment variable.')

engine = create_engine(db_url, echo=True)
Session = sessionmaker(bind=engine)

try:
    with Session() as db:
        one = db.query(1).one()
        if one[0] != 1:
            raise Exception('Invalid database return value')
except BaseException as e:
    raise Exception(f'Invalid database connection {db_url}') from e
