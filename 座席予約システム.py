import os
import sqlite3
import random
import string
import re
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template_string, request, jsonify, redirect, url_for
import stripe
import psycopg2
import urllib.parse

app = Flask(__name__)

# ---------------------------------------------------------
# データベース接続設定 (PostgreSQL対応)
# ---------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    if DATABASE_URL:
        # PostgreSQL (Renderの本番環境)
        url = urllib.parse.urlparse(DATABASE_URL)
        conn = psycopg2.connect(
            database=url.path[1:],
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port
        )
        return conn
    else:
        # ローカルテスト用 SQLite
        conn = sqlite3.connect("seats.db")
        return conn

# ---------------------------------------------------------
# APIキー・認証設定
# ---------------------------------------------------------
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "pk_live_51UMcuZCoyCE0ABRGMHV0gxvqSgBowMRrzzkRI3fBY1yJorvWoDBMopP4LEPiAIJJEls4PdtdlWC0tjL80thjKVkP00okT4Xdk0")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_whsec_uHiYJvTzobfOKJoO2KL0y1Sx6mB3SCa0")
stripe.api_key = STRIPE_API_KEY

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "project0106korin@gmail.com")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "tvrs ksrb dwbh dcio")

TICKET_PRICE = 1000 
FEE_CONFIG = {
    'stripe': 100,      
    'convenience': 220, 
    'member': 0         
}

VALID_MEMBER_IDS = [
    "15212", "16318", "16537", "16624", "16829", 
    "17313", "17315", "17325", "17417", "17422", 
    "17528", "17735", "18010", "18319", "18323", 
    "18721", "18834", "19111", "19430", "19626", 
    "19630", "19731", "20314", "20320", "20332", "20733"
]

def generate_booking_code():
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=6))

def is_katakana(text):
    pattern = re.compile(r'^[\u30A1-\u30FC\s]+$')
    return bool(pattern.match(text))

def is_reserved_seat(row, num):
    reserved_rows = ['F', 'G', 'H', 'I', 'J', 'K', 'L']
    return (row in reserved_rows) and (5 <= num <= 16)

def send_confirmation_email(to_email, name, booking_code, perf_time, seat_str, total_price):
    if not SMTP_EMAIL:
        return
    
    perf_name = "昼公演 (13:00開演)" if perf_time == 'day' else "夜公演 (17:00開演)"
    
    subject = "【虹凛プロジェクト】チケット予約・購入完了のお知らせ"
    body = f"""{name} 様

この度は『虹凛プロジェクト』公演チケットをご予約・ご購入いただき誠にありがとうございます。
決済および予約手続きが正常に完了いたしました。

■ ご予約内容
----------------------------------------
予約コード: {booking_code}
対象公演: {perf_name}
座席情報: {seat_str}
合計金額: ¥{total_price:,}
----------------------------------------

当日は受付にてこちらの予約コード（{booking_code}）をお手元にご準備の上、ご提示をお願いいたします。

皆様のご来場を心よりお待ちしております。

----------------------------------------
虹凛プロジェクト 制作部
メール: {SMTP_EMAIL}
"""

    msg = MIMEMultipart()
    msg['From'] = SMTP_EMAIL
    msg['To'] = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"メール送信エラー: {e}")

# ---------------------------------------------------------
# データベース初期化
# ---------------------------------------------------------
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    # PostgreSQLとSQLite両対応の型・構文
    if DATABASE_URL:
        c.execute('''
            CREATE TABLE IF NOT EXISTS seats (
                id SERIAL PRIMARY KEY,
                performance_time TEXT DEFAULT 'day',
                seat_number TEXT,
                row_label TEXT,
                seat_num INT,
                seat_type TEXT,
                status TEXT DEFAULT 'available',
                purchased_by TEXT,
                email TEXT,
                booking_code TEXT,
                payment_method TEXT,
                member_id TEXT,
                expires_at TEXT
            )
        ''')
    else:
        c.execute('''
            CREATE TABLE IF NOT EXISTS seats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                performance_time TEXT DEFAULT 'day',
                seat_number TEXT,
                row_label TEXT,
                seat_num INT,
                seat_type TEXT,
                status TEXT DEFAULT 'available',
                purchased_by TEXT,
                email TEXT,
                booking_code TEXT,
                payment_method TEXT,
                member_id TEXT,
                expires_at TEXT
            )
        ''')
    
    # データ件数確認
    if DATABASE_URL:
        c.execute('SELECT COUNT(*) FROM seats')
    else:
        c.execute('SELECT COUNT(*) FROM seats')
    
    count = c.fetchone()[0]
    if count == 0:
        rows = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O']
        performances = ['day', 'night']
        for perf in performances:
            for r in rows:
                for num in range(1, 21):
                    seat_num_str = f"{r}-{num}"
                    stype = 'reserved' if is_reserved_seat(r, num) else 'unreserved'
                    status = 'sold' if r in ['M', 'N', 'O'] else 'available'
                    
                    if DATABASE_URL:
                        c.execute('INSERT INTO seats (performance_time, seat_number, row_label, seat_num, seat_type, status, purchased_by, email, booking_code, payment_method, member_id, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                                  (perf, seat_num_str, r, num, stype, status, '', '', '', '', '', None))
                    else:
                        c.execute('INSERT INTO seats (performance_time, seat_number, row_label, seat_num, seat_type, status, purchased_by, email, booking_code, payment_method, member_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                  (perf, seat_num_str, r, num, stype, status, '', '', '', '', '', None))
        conn.commit()
    conn.close()

init_db()

def release_expired_seats():
    conn = get_db_connection()
    c = conn.cursor()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if DATABASE_URL:
        c.execute('''
            UPDATE seats 
            SET status = 'available', purchased_by = '', email = '', booking_code = '', payment_method = '', member_id = '', expires_at = NULL 
            WHERE status = 'pending_payment' AND expires_at IS NOT NULL AND expires_at < %s
        ''', (now_str,))
    else:
        c.execute('''
            UPDATE seats 
            SET status = 'available', purchased_by = '', email = '', booking_code = '', payment_method = '', member_id = '', expires_at = NULL 
            WHERE status = 'pending_payment' AND expires_at IS NOT NULL AND expires_at < ?
        ''', (now_str,))
        
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# HTMLテンプレート（以前と同じ）
# ---------------------------------------------------------
HTML_BUY = """...""" # （※文字数節約のため、中身は以前のまま変更なしでOKです。コピペの際は直前のHTMLコードを入れてください）
HTML_ADMIN = """..."""

# （※HTML部分は省略せず、お手元のコードのHTML部分をそのまま維持してください）

# ---------------------------------------------------------
# ルート定義（トップページ・購入ページ）
# ---------------------------------------------------------
@app.route('/')
def index():
    # アクセス時に期限切れの仮予約を自動で解放する
    release_expired_seats()
    
    # データベースから座席の予約状況を取得してHTML_BUYを表示する例
    conn = get_db_connection()
    c = conn.cursor()
    
    # 必要に応じて公演時間（day/night）などのパラメータを受け取る処理をここに記述
    perf = request.args.get('perf', 'day')
    
    if DATABASE_URL:
        c.execute('SELECT seat_number, status, seat_type FROM seats WHERE performance_time = %s', (perf,))
    else:
        c.execute('SELECT seat_number, status, seat_type FROM seats WHERE performance_time = ?', (perf,))
        
    seats = c.fetchall()
    conn.close()
    
    # HTML_BUYテンプレートを描画して返す
    return render_template_string(HTML_BUY, seats=seats, perf=perf)
