"""Public legal pages (Impressum, Datenschutz, AGB, Widerruf) -- no auth,
no forms, pure static render_template. Linked from the landing page and
from every account_ui page's footer.
"""

from flask import Blueprint, render_template

legal_ui_bp = Blueprint("legal_ui", __name__, url_prefix="/legal", template_folder="templates")


@legal_ui_bp.get("/impressum")
def impressum():
    return render_template("legal_ui/impressum.html")


@legal_ui_bp.get("/datenschutz")
def datenschutz():
    return render_template("legal_ui/datenschutz.html")


@legal_ui_bp.get("/agb")
def agb():
    return render_template("legal_ui/agb.html")


@legal_ui_bp.get("/widerruf")
def widerruf():
    return render_template("legal_ui/widerruf.html")
