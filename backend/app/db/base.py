from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Import models so Alembic sees every table from Base.metadata.
from app.db import models as models  # noqa: E402,F401
