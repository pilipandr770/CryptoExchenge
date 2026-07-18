import os

from dotenv import load_dotenv

# Load .env relative to this file, not the process cwd -- `flask run`'s own
# .env auto-load already does this, but `python wsgi.py` doesn't, so this
# makes both entry points behave the same way.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from app import create_app  # noqa: E402 -- must follow load_dotenv()

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
