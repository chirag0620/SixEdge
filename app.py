import os
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, send_from_directory, abort, session
)
from werkzeug.utils import secure_filename
from sqlalchemy import func

from config import Config
from models import db, User, Market, Trade, CATEGORY_CHOICES, EMOTION_CHOICES, STRATEGY_CHOICES


try:
    from authlib.integrations.flask_client import OAuth
    has_authlib = True
except ImportError:
    has_authlib = False


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    oauth = None
    if has_authlib:
        oauth = OAuth(app)
        if app.config.get("GOOGLE_CLIENT_ID") and app.config.get("GOOGLE_CLIENT_SECRET"):
            oauth.register(
                name="google",
                client_id=app.config["GOOGLE_CLIENT_ID"],
                client_secret=app.config["GOOGLE_CLIENT_SECRET"],
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_kwargs={"scope": "openid email profile"},
                claims_options={
                    "iat": {"essential": False}
                }
            )

    with app.app_context():
        db.create_all()

        # Ensure user_id column exists if table was previously created in older schema
        try:
            db.session.execute(db.text("ALTER TABLE trades ADD COLUMN user_id INTEGER REFERENCES users(id)"))
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Ensure strategy column exists if table was previously created in older schema
        try:
            db.session.execute(db.text("ALTER TABLE trades ADD COLUMN strategy VARCHAR(50)"))
            db.session.commit()
        except Exception:
            db.session.rollback()

        seed_default_markets()
        seed_admin_user()

    register_routes(app, oauth=oauth)
    return app



def seed_default_markets():
    """Populate a starter set of markets on first run, if the table is empty."""
    if Market.query.first():
        return
    defaults = [
        ("Gold", "XAU/USD", "Metals"),
        ("Silver", "XAG/USD", "Metals"),
        ("Bitcoin", "BTC/USD", "Crypto"),
        ("Ethereum", "ETH/USD", "Crypto"),
        ("USD/JPY", "USD/JPY", "Forex"),
        ("EUR/USD", "EUR/USD", "Forex"),
        ("GBP/USD", "GBP/USD", "Forex"),
        ("US30", "US30", "Indices"),
        ("NASDAQ 100", "NAS100", "Indices"),
    ]
    for name, symbol, category in defaults:
        db.session.add(Market(name=name, symbol=symbol, category=category))
    db.session.commit()


def seed_admin_user():
    """Create a default admin user if not present and assign unassigned trades to admin."""
    admin = User.query.filter_by(username="admin").first()
    if not admin:
        admin = User(
            username="admin",
            email="admin@tradeledger.com",
            provider="custom"
        )
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()

    # Assign any legacy unassigned trades to admin so they are isolated from other users
    Trade.query.filter(Trade.user_id.is_(None)).update({"user_id": admin.id})
    db.session.commit()


def allowed_file(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in Config.ALLOWED_EXTENSIONS


def to_date(value, fallback):
    """Parse a yyyy-mm-dd form field into a date object, falling back if blank/invalid."""
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def to_decimal(value):
    """Parse a form field into a Decimal, returning None for blank/invalid input."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def save_trade_image(file_storage):
    """Save an uploaded screenshot with a unique filename. Returns the filename or None."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        flash("Image type not supported. Use png, jpg, jpeg, gif, or webp.", "error")
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(Config.UPLOAD_FOLDER, unique_name))
    return unique_name


# ---------------------------------------------------------------------------
# Consistency / discipline calculations
# ---------------------------------------------------------------------------

def compute_streak(trade_dates):
    """
    Given a set of dates on which at least one trade was journaled,
    return (current_streak, longest_streak) counted in consecutive days.
    The current streak counts back from today (or yesterday, so that not
    having logged *today yet* doesn't zero it out).
    """
    if not trade_dates:
        return 0, 0

    days = sorted(trade_dates)
    longest = 1
    run = 1
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1

    today = date.today()
    day_set = set(days)
    if today in day_set:
        anchor = today
    elif (today - timedelta(days=1)) in day_set:
        anchor = today - timedelta(days=1)
    else:
        return 0, longest

    current = 0
    cursor = anchor
    while cursor in day_set:
        current += 1
        cursor -= timedelta(days=1)

    return current, longest


def build_heatmap(trade_dates, weeks=18):
    """Return a list of {date, count} dicts for the last `weeks` weeks, oldest first."""
    counts = {}
    for d in trade_dates:
        counts[d] = counts.get(d, 0) + 1

    total_days = weeks * 7
    start = date.today() - timedelta(days=total_days - 1)
    cells = []
    for i in range(total_days):
        d = start + timedelta(days=i)
        cells.append({"date": d.isoformat(), "count": counts.get(d, 0)})
    return cells


def register_routes(app, oauth=None):

    @app.context_processor
    def inject_user_and_greeting():
        user_id = session.get("user_id")
        current_user = User.query.get(user_id) if user_id else None

        hour = datetime.now().hour
        if 5 <= hour < 12:
            time_greeting = "Good morning"
        elif 12 <= hour < 17:
            time_greeting = "Good afternoon"
        elif 17 <= hour < 22:
            time_greeting = "Good evening"
        else:
            time_greeting = "Good night"

        return dict(current_user=current_user, time_greeting=time_greeting)

    def login_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if "user_id" not in session:
                flash("Please sign in to access your trading journal.", "error")
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated_function

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if "user_id" in session:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not username or not email or not password:
                flash("All fields are required.", "error")
            elif password != confirm_password:
                flash("Passwords do not match.", "error")
            elif User.query.filter_by(username=username).first():
                flash("Username is already taken.", "error")
            elif User.query.filter_by(email=email).first():
                flash("Email is already registered.", "error")
            else:
                user = User(username=username, email=email, provider="custom")
                user.set_password(password)
                db.session.add(user)
                db.session.commit()

                session["user_id"] = user.id
                flash(f"Account created! Welcome, {user.username}.", "success")
                return redirect(url_for("dashboard"))

        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "user_id" in session:
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            login_id = request.form.get("login_id", "").strip()
            password = request.form.get("password", "")

            user = User.query.filter(
                (User.username == login_id) | (User.email == login_id.lower())
            ).first()

            if not user or not user.check_password(password):
                flash("Invalid username/email or password.", "error")
            else:
                session["user_id"] = user.id
                flash(f"Welcome back, {user.username}!", "success")
                return redirect(url_for("dashboard"))

        return render_template("login.html")

    @app.route("/login/google")
    def login_google():
        client_id = app.config.get("GOOGLE_CLIENT_ID")
        client_secret = app.config.get("GOOGLE_CLIENT_SECRET")

        if oauth and client_id and client_secret and "your_google_client_id" not in client_id:
            redirect_uri = url_for("login_google_authorized", _external=True)
            return oauth.google.authorize_redirect(redirect_uri)
        
        flash("Google OAuth credentials set up nahi hain (.env file me GOOGLE_CLIENT_ID add karein). Demo account signed in.", "error")
        user = User.query.filter_by(email="google.trader@example.com").first()
        if not user:
            user = User(
                username="Google Trader",
                email="google.trader@example.com",
                provider="google"
            )
            db.session.add(user)
            db.session.commit()

        session["user_id"] = user.id
        return redirect(url_for("dashboard"))

    @app.route("/login/google/authorized")
    def login_google_authorized():
        if not oauth:
            return redirect(url_for("login"))
        try:
            token = oauth.google.authorize_access_token(leeway=3600)
            user_info = token.get("userinfo")

            if not user_info:
                resp = oauth.google.get("https://www.googleapis.com/oauth2/v3/userinfo", token=token)
                if resp.ok:
                    user_info = resp.json()

            if user_info and user_info.get("email"):
                email = user_info.get("email").lower()
                name = user_info.get("name") or email.split("@")[0]
                user = User.query.filter_by(email=email).first()
                if not user:
                    user = User(username=name, email=email, provider="google")
                    db.session.add(user)
                    db.session.commit()
                session["user_id"] = user.id
                flash(f"Signed in with Google as {user.email}!", "success")
                return redirect(url_for("dashboard"))
        except Exception as e:
            try:
                resp = oauth.google.get("https://www.googleapis.com/oauth2/v3/userinfo")
                if resp.ok:
                    user_info = resp.json()
                    if user_info and user_info.get("email"):
                        email = user_info.get("email").lower()
                        name = user_info.get("name") or email.split("@")[0]
                        user = User.query.filter_by(email=email).first()
                        if not user:
                            user = User(username=name, email=email, provider="google")
                            db.session.add(user)
                            db.session.commit()
                        session["user_id"] = user.id
                        flash(f"Signed in with Google as {user.email}!", "success")
                        return redirect(url_for("dashboard"))
            except Exception:
                pass
            flash(f"Google login error: {str(e)}", "error")
        return redirect(url_for("login"))


    @app.route("/login/apple")
    def login_apple():
        user = User.query.filter_by(email="apple.trader@example.com").first()
        if not user:
            user = User(
                username="Apple Trader",
                email="apple.trader@example.com",
                provider="apple"
            )
            db.session.add(user)
            db.session.commit()

        session["user_id"] = user.id
        flash(f"Signed in with Apple as {user.username}!", "success")
        return redirect(url_for("dashboard"))

    @app.route("/logout")
    def logout():
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("login"))

    @app.route("/")
    def dashboard():
        user_id = session.get("user_id")
        if user_id:
            query = Trade.query.filter(Trade.user_id == user_id)
        else:
            query = Trade.query.filter(Trade.user_id.is_(None))

        trades = query.order_by(Trade.trade_date.desc(), Trade.id.desc()).all()
        total_trades = len(trades)

        closed = [t for t in trades if t.outcome in ("win", "loss", "breakeven")]
        wins = [t for t in closed if t.outcome == "win"]
        win_rate = round(100 * len(wins) / len(closed), 1) if closed else None

        rr_values = [float(t.risk_reward_ratio) for t in trades if t.risk_reward_ratio is not None]
        avg_rr = round(sum(rr_values) / len(rr_values), 2) if rr_values else None

        rule_trades = [t for t in trades if t.rule_followed is not None]
        discipline_score = (
            round(100 * sum(1 for t in rule_trades if t.rule_followed) / len(rule_trades), 1)
            if rule_trades else None
        )

        total_pnl = sum(float(t.pnl) for t in trades if t.pnl is not None)

        trade_dates = [t.trade_date for t in trades]
        current_streak, longest_streak = compute_streak(trade_dates)
        heatmap = build_heatmap(trade_dates)

        by_market = (
            db.session.query(Market.name, func.count(Trade.id))
            .join(Trade, Trade.market_id == Market.id)
            .filter(Trade.user_id == user_id if user_id else Trade.user_id.is_(None))
            .group_by(Market.name)
            .order_by(func.count(Trade.id).desc())
            .all()
        )

        # Build Equity Curve data (chronological)
        chrono_trades = [t for t in sorted(trades, key=lambda x: (x.trade_date, x.id)) if t.pnl is not None]
        equity_labels = []
        equity_values = []
        cum_pnl = 0.0
        for t in chrono_trades:
            cum_pnl += float(t.pnl)
            equity_labels.append(t.trade_date.isoformat())
            equity_values.append(round(cum_pnl, 2))

        # Strategy performance stats
        strategy_stats = []
        for strat in STRATEGY_CHOICES:
            strat_trades = [t for t in trades if t.strategy == strat]
            if strat_trades:
                strat_closed = [t for t in strat_trades if t.outcome in ("win", "loss", "breakeven")]
                strat_wins = sum(1 for t in strat_closed if t.outcome == "win")
                strat_win_rate = round(100 * strat_wins / len(strat_closed), 1) if strat_closed else 0.0
                strat_pnl = round(sum(float(t.pnl) for t in strat_trades if t.pnl is not None), 2)
                strategy_stats.append({
                    "name": strat,
                    "count": len(strat_trades),
                    "win_rate": strat_win_rate,
                    "net_pnl": strat_pnl
                })

        recent_trades = trades[:8]

        return render_template(
            "dashboard.html",
            total_trades=total_trades,
            win_rate=win_rate,
            avg_rr=avg_rr,
            discipline_score=discipline_score,
            total_pnl=total_pnl,
            current_streak=current_streak,
            longest_streak=longest_streak,
            heatmap=heatmap,
            by_market=by_market,
            recent_trades=recent_trades,
            equity_labels=equity_labels,
            equity_values=equity_values,
            strategy_stats=strategy_stats,
        )

    @app.route("/trades")
    def trades_list():
        user_id = session.get("user_id")
        market_id = request.args.get("market_id", type=int)
        direction = request.args.get("direction")
        outcome = request.args.get("outcome")
        strategy = request.args.get("strategy")

        if user_id:
            query = Trade.query.filter(Trade.user_id == user_id)
        else:
            query = Trade.query.filter(Trade.user_id.is_(None))

        if market_id:
            query = query.filter(Trade.market_id == market_id)
        if direction in ("buy", "sell"):
            query = query.filter(Trade.direction == direction)
        if outcome in ("win", "loss", "breakeven", "open"):
            query = query.filter(Trade.outcome == outcome)
        if strategy:
            query = query.filter(Trade.strategy == strategy)

        trades = query.order_by(Trade.trade_date.desc(), Trade.id.desc()).all()
        markets = Market.query.order_by(Market.name).all()
        return render_template(
            "trades.html", trades=trades, markets=markets,
            strategies=STRATEGY_CHOICES,
            selected_market=market_id, selected_direction=direction,
            selected_outcome=outcome, selected_strategy=strategy,
        )

    @app.route("/trades/new", methods=["GET", "POST"])
    def trade_new():
        markets = Market.query.order_by(Market.name).all()
        if not markets:
            flash("Add a market first before logging a trade.", "error")
            return redirect(url_for("markets_page"))

        if request.method == "POST":
            trade = Trade(
                user_id=session.get("user_id"),
                market_id=request.form.get("market_id", type=int),
                trade_date=to_date(request.form.get("trade_date"), date.today()),
                direction=request.form.get("direction"),
                entry_price=to_decimal(request.form.get("entry_price")),
                stop_loss=to_decimal(request.form.get("stop_loss")),
                take_profit=to_decimal(request.form.get("take_profit")),
                exit_price=to_decimal(request.form.get("exit_price")),
                lot_size=to_decimal(request.form.get("lot_size")),
                outcome=request.form.get("outcome", "open"),
                pnl=to_decimal(request.form.get("pnl")),
                emotion=request.form.get("emotion") or None,
                strategy=request.form.get("strategy") or None,
                rule_followed=request.form.get("rule_followed") == "on",
                notes=request.form.get("notes"),
            )

            manual_rr = to_decimal(request.form.get("risk_reward_ratio"))
            trade.risk_reward_ratio = manual_rr if manual_rr is not None else trade.compute_rr()

            trade.image_filename = save_trade_image(request.files.get("image"))

            if not trade.market_id or trade.direction not in ("buy", "sell"):
                flash("Market and buy/sell direction are required.", "error")
                return render_template("trade_form.html", markets=markets,
                                        emotions=EMOTION_CHOICES, strategies=STRATEGY_CHOICES,
                                        trade=None, today=date.today().isoformat())

            db.session.add(trade)
            db.session.commit()
            flash("Trade logged.", "success")
            return redirect(url_for("trades_list"))

        return render_template("trade_form.html", markets=markets,
                                emotions=EMOTION_CHOICES, strategies=STRATEGY_CHOICES,
                                trade=None, today=date.today().isoformat())

    @app.route("/trades/<int:trade_id>")
    def trade_detail(trade_id):
        trade = Trade.query.get_or_404(trade_id)
        user_id = session.get("user_id")
        if trade.user_id and trade.user_id != user_id:
            flash("You do not have permission to view this trade.", "error")
            return redirect(url_for("trades_list"))
        return render_template("trade_detail.html", trade=trade)

    @app.route("/trades/<int:trade_id>/edit", methods=["GET", "POST"])
    def trade_edit(trade_id):
        trade = Trade.query.get_or_404(trade_id)
        user_id = session.get("user_id")
        if trade.user_id and trade.user_id != user_id:
            flash("You do not have permission to edit this trade.", "error")
            return redirect(url_for("trades_list"))

        markets = Market.query.order_by(Market.name).all()

        if request.method == "POST":
            trade.market_id = request.form.get("market_id", type=int)
            trade.trade_date = to_date(request.form.get("trade_date"), trade.trade_date)
            trade.direction = request.form.get("direction")
            trade.entry_price = to_decimal(request.form.get("entry_price"))
            trade.stop_loss = to_decimal(request.form.get("stop_loss"))
            trade.take_profit = to_decimal(request.form.get("take_profit"))
            trade.exit_price = to_decimal(request.form.get("exit_price"))
            trade.lot_size = to_decimal(request.form.get("lot_size"))
            trade.outcome = request.form.get("outcome", "open")
            trade.pnl = to_decimal(request.form.get("pnl"))
            trade.emotion = request.form.get("emotion") or None
            trade.strategy = request.form.get("strategy") or None
            trade.rule_followed = request.form.get("rule_followed") == "on"
            trade.notes = request.form.get("notes")

            manual_rr = to_decimal(request.form.get("risk_reward_ratio"))
            trade.risk_reward_ratio = manual_rr if manual_rr is not None else trade.compute_rr()

            new_image = save_trade_image(request.files.get("image"))
            if new_image:
                trade.image_filename = new_image

            db.session.commit()
            flash("Trade updated.", "success")
            return redirect(url_for("trade_detail", trade_id=trade.id))

        return render_template("trade_form.html", markets=markets,
                                emotions=EMOTION_CHOICES, strategies=STRATEGY_CHOICES,
                                trade=trade, today=trade.trade_date.isoformat())

    @app.route("/trades/<int:trade_id>/delete", methods=["POST"])
    def trade_delete(trade_id):
        trade = Trade.query.get_or_404(trade_id)
        user_id = session.get("user_id")
        if trade.user_id and trade.user_id != user_id:
            flash("You do not have permission to delete this trade.", "error")
            return redirect(url_for("trades_list"))

        db.session.delete(trade)
        db.session.commit()
        flash("Trade deleted.", "success")
        return redirect(url_for("trades_list"))

    @app.route("/markets", methods=["GET", "POST"])
    def markets_page():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            symbol = request.form.get("symbol", "").strip()
            category = request.form.get("category", "Other")
            if not name or not symbol:
                flash("Market name and symbol are required.", "error")
            elif Market.query.filter_by(symbol=symbol).first():
                flash(f"{symbol} already exists.", "error")
            else:
                db.session.add(Market(name=name, symbol=symbol, category=category))
                db.session.commit()
                flash(f"Added {name} ({symbol}).", "success")
            return redirect(url_for("markets_page"))

        markets = Market.query.order_by(Market.category, Market.name).all()
        return render_template("markets.html", markets=markets, categories=CATEGORY_CHOICES)

    @app.route("/markets/<int:market_id>/delete", methods=["POST"])
    def market_delete(market_id):
        market = Market.query.get_or_404(market_id)
        if market.trades:
            flash("Can't delete a market that already has trades logged against it.", "error")
        else:
            db.session.delete(market)
            db.session.commit()
            flash("Market removed.", "success")
        return redirect(url_for("markets_page"))

    @app.route("/uploads/<filename>")
    def uploaded_file(filename):
        return send_from_directory(Config.UPLOAD_FOLDER, filename)


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
