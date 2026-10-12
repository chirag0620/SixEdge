from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

CATEGORY_CHOICES = ["Forex", "Metals", "Crypto", "Indices", "Stocks", "Other"]
DIRECTION_CHOICES = ["buy", "sell"]
OUTCOME_CHOICES = ["win", "loss", "breakeven", "open"]
EMOTION_CHOICES = [
    "calm", "confident", "fearful", "greedy", "fomo",
    "revenge", "bored", "impatient", "disciplined",
]


STRATEGY_CHOICES = [
    "Breakout", "ICT / FVG", "Support & Resistance",
    "Trend Following", "Scalping", "Pullback", "Supply & Demand", "Other"
]


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False, unique=True)
    email = db.Column(db.String(120), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=True)  # None for social logins
    provider = db.Column(db.String(20), default="custom")  # custom, google, apple
    avatar_url = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    trades = db.relationship(
        "Trade", backref="user", lazy=True, cascade="all, delete-orphan"
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username}>"


class Market(db.Model):
    __tablename__ = "markets"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False, unique=True)   # e.g. "Gold"
    symbol = db.Column(db.String(20), nullable=False, unique=True)  # e.g. "XAU/USD"
    category = db.Column(db.String(20), nullable=False, default="Other")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    trades = db.relationship(
        "Trade", backref="market", lazy=True, cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Market {self.symbol}>"


class Trade(db.Model):
    __tablename__ = "trades"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    market_id = db.Column(db.Integer, db.ForeignKey("markets.id"), nullable=False)

    trade_date = db.Column(db.Date, nullable=False, default=date.today)
    direction = db.Column(db.String(4), nullable=False)  # buy / sell

    entry_price = db.Column(db.Numeric(18, 6))
    stop_loss = db.Column(db.Numeric(18, 6))
    take_profit = db.Column(db.Numeric(18, 6))
    exit_price = db.Column(db.Numeric(18, 6))

    lot_size = db.Column(db.Numeric(12, 4))
    risk_reward_ratio = db.Column(db.Numeric(8, 2))  # e.g. 2.50 means 1:2.5

    outcome = db.Column(db.String(10), nullable=False, default="open")
    pnl = db.Column(db.Numeric(14, 2))  # profit/loss in account currency

    emotion = db.Column(db.String(20))
    strategy = db.Column(db.String(50))
    rule_followed = db.Column(db.Boolean, default=True, nullable=False)
    notes = db.Column(db.Text)

    image_filename = db.Column(db.String(255))  # screenshot of the setup/chart

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def compute_rr(self):
        """Auto-calculate risk:reward from prices if not explicitly set."""
        if self.entry_price is None or self.stop_loss is None:
            return None
        risk = abs(float(self.entry_price) - float(self.stop_loss))
        if risk == 0:
            return None
        if self.take_profit is not None:
            reward = abs(float(self.take_profit) - float(self.entry_price))
        elif self.exit_price is not None:
            reward = abs(float(self.exit_price) - float(self.entry_price))
        else:
            return None
        return round(reward / risk, 2)

    def __repr__(self):
        return f"<Trade {self.id} {self.direction} {self.market_id}>"
