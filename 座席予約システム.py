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
import json

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
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_ecbizSk8A82Ef7p7lC9ponR6OEjeWrJ7")
stripe.api_key = STRIPE_API_KEY
# 一時的に直接URLを書き込む場合（[パスワード]をご自身のものに変更してください）

def get_db_connection():
    if DATABASE_URL:
        # URLをパースする代わりに、個別の要素として安全に渡す
        url = urllib.parse.urlparse(DATABASE_URL)
        conn = psycopg2.connect(
            dbname=url.path.lstrip('/'),
            user=url.username,
            password=url.password,
            host=url.hostname,
            port=url.port or 5432
        )
        return conn
    else:
        # ローカルテスト用 SQLite
        conn = sqlite3.connect("seats.db")
        return conn
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

def normalize_phone(text):
    """全角を半角にし、ハイフンやスペースをすべて削除して数字のみにする"""
    if not text:
        return ""
    translator = str.maketrans(
        ''.join(chr(i) for i in range(0xFF01, 0xFF5F)),
        ''.join(chr(i) for i in range(0x21, 0x7F))
    )
    normalized = text.translate(translator)
    normalized = re.sub(r'[\s\-ー―]', '', normalized)
    return normalized.strip()

def is_phone_number(text):
    pattern = re.compile(r'^\d{10,11}$')
    return bool(pattern.match(text))

def is_reserved_seat(row, num):
    reserved_rows = ['F', 'G', 'H', 'I', 'J', 'K', 'L']
    return (row in reserved_rows) and (5 <= num <= 16)

# ---------------------------------------------------------
# データベース初期化
# ---------------------------------------------------------
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
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
                phone TEXT,
                password TEXT,
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
                phone TEXT,
                password TEXT,
                booking_code TEXT,
                payment_method TEXT,
                member_id TEXT,
                expires_at TEXT
            )
        ''')
    
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
                        c.execute('INSERT INTO seats (performance_time, seat_number, row_label, seat_num, seat_type, status, purchased_by, phone, password, booking_code, payment_method, member_id, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                                  (perf, seat_num_str, r, num, stype, status, '', '', '', '', '', '', None))
                    else:
                        c.execute('INSERT INTO seats (performance_time, seat_number, row_label, seat_num, seat_type, status, purchased_by, phone, password, booking_code, payment_method, member_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                  (perf, seat_num_str, r, num, stype, status, '', '', '', '', '', '', None))
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
            SET status = 'available', purchased_by = '', phone = '', password = '', booking_code = '', payment_method = '', member_id = '', expires_at = NULL 
            WHERE status = 'pending_payment' AND expires_at IS NOT NULL AND expires_at < %s
        ''', (now_str,))
    else:
        c.execute('''
            UPDATE seats 
            SET status = 'available', purchased_by = '', phone = '', password = '', booking_code = '', payment_method = '', member_id = '', expires_at = NULL 
            WHERE status = 'pending_payment' AND expires_at IS NOT NULL AND expires_at < ?
        ''', (now_str,))
        
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# HTMLテンプレート（購入画面 兼 マイページ）
# ---------------------------------------------------------
HTML_BUY = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>昭和文化小劇場 - 座席予約 | 虹凛プロジェクト</title>
    <style>
        body { font-family: sans-serif; text-align: center; padding: 10px; background: #f4f4f9; margin: 0; }
        h1 { font-size: 20px; margin: 10px 0; }
        
        .top-nav { display: flex; justify-content: center; gap: 10px; margin-bottom: 15px; }
        .nav-btn { padding: 8px 16px; font-size: 13px; font-weight: bold; border: 1px solid #007bff; background: white; color: #007bff; border-radius: 20px; cursor: pointer; }
        .nav-btn.active { background: #007bff; color: white; }

        .perf-tabs { display: flex; justify-content: center; gap: 10px; margin-bottom: 15px; }
        .perf-tab { flex: 1; max-width: 200px; padding: 12px; font-weight: bold; border: 2px solid #333; border-radius: 8px; background: white; color: #333; cursor: pointer; font-size: 14px; }
        .perf-tab.active { background: #333; color: white; border-color: #333; }

        .stage { background: #444; color: white; padding: 8px; width: 80%; max-width: 500px; margin: 0 auto 15px; border-radius: 4px; font-weight: bold; letter-spacing: 2px; }
        
        .theater-container { overflow-x: auto; padding: 10px 0; display: flex; justify-content: center; }
        .seating-chart { display: flex; flex-direction: column; gap: 6px; min-width: 580px; }
        .seat-row { display: flex; align-items: center; justify-content: center; gap: 4px; }
        .block-left, .block-center, .block-right { display: flex; gap: 3px; }
        .row-label { width: 22px; font-weight: bold; color: #333; font-size: 13px; text-align: center; }
        
        .seat { width: 22px; height: 26px; line-height: 26px; border-radius: 3px; font-size: 10px; cursor: pointer; user-select: none; font-weight: bold; color: white; }
        .seat.reserved-seat { background: #2980b9; }
        .seat.unreserved-seat { background: #95a5a6; cursor: default; opacity: 0.6; }
        .seat.sold { background: #e74c3c !important; cursor: not-allowed; opacity: 1; }
        .seat.pending_payment { background: #f39c12 !important; cursor: not-allowed; opacity: 0.7; }
        .seat.selected { background: #f39c12 !important; outline: 2px solid #d35400; opacity: 1; }
        
        .aisle-h { width: 12px; }
        .aisle-v { height: 20px; width: 100%; }

        .legend { display: flex; justify-content: center; gap: 12px; margin: 12px 0; font-size: 12px; flex-wrap: wrap; }
        .legend-item { display: flex; align-items: center; gap: 5px; }
        .legend-box { width: 15px; height: 15px; border-radius: 3px; }
        
        .checkout, .mypage-box { background: white; padding: 15px; border-radius: 8px; max-width: 420px; margin: 10px auto; box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: left; }
        .mode-switch { margin-bottom: 15px; }
        .mode-btn { padding: 8px 12px; font-size: 12px; border: 1px solid #007bff; background: white; color: #007bff; border-radius: 4px; cursor: pointer; }
        .mode-btn.active { background: #007bff; color: white; font-weight: bold; }
        
        .form-group { margin: 10px 0; }
        .form-group label { font-size: 13px; font-weight: bold; display: block; margin-bottom: 4px; color: #333; }
        input, select, button { padding: 10px; font-size: 14px; border-radius: 4px; border: 1px solid #ccc; box-sizing: border-box; width: 100%; }
        button.submit-btn { background: #007bff; color: white; border: none; font-weight: bold; cursor: pointer; margin-top: 10px; }
        
        .modal { display: none; position: fixed; z-index: 100; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.5); overflow-y: auto; }
        .modal-content { background-color: white; margin: 5% auto; padding: 20px; border-radius: 8px; width: 85%; max-width: 440px; text-align: left; box-sizing: border-box; }
        .code-display { font-size: 24px; font-weight: bold; color: #e74c3c; background: #f8d7da; padding: 10px; border-radius: 5px; margin: 15px 0; letter-spacing: 3px; text-align: center; }
        
        .invoice-box { background: #f8f9fa; border: 1px dashed #ccc; padding: 12px; border-radius: 6px; margin: 10px 0; font-size: 13px; }
        .invoice-row { display: flex; justify-content: space-between; margin: 4px 0; }
        .invoice-total { display: flex; justify-content: space-between; margin-top: 8px; padding-top: 8px; border-top: 2px solid #333; font-weight: bold; font-size: 15px; color: #d35400; }
        
        .terms-box { background: #fdfdfd; border: 1px solid #ccc; font-size: 11px; padding: 10px; height: 110px; overflow-y: scroll; margin: 10px 0; color: #333; line-height: 1.5; white-space: pre-wrap; }
        .agree-check { display: flex; align-items: center; gap: 8px; font-size: 12px; font-weight: bold; margin: 10px 0; }
        .agree-check input { width: auto; cursor: pointer; }

        .tokusho-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 12px; }
        .tokusho-table th, .tokusho-table td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        .tokusho-table th { background: #f1f1f1; width: 35%; }

        .footer-links { margin: 25px 0 15px; font-size: 12px; color: #666; text-align: center; }
        .footer-links a { color: #007bff; text-decoration: underline; cursor: pointer; }
        
        .ticket-card { background: #fffcf0; border: 2px solid #f39c12; border-radius: 6px; padding: 12px; margin-bottom: 12px; font-size: 13px; }
    </style>
</head>
<body>
    <h1>昭和文化小劇場 座席予約</h1>

    <div class="top-nav">
        <button id="navBooking" class="nav-btn active" onclick="switchMainTab('booking')">🎫 チケットを予約・購入する</button>
        <button id="navMypage" class="nav-btn" onclick="switchMainTab('mypage')">👤 マイページ（予約確認）</button>
    </div>

    <!-- 予約画面セクション -->
    <div id="sectionBooking">
        <div class="perf-tabs">
            <button id="tabDay" class="perf-tab active" onclick="switchPerformance('day')">☀ 昼公演<br><small>(14:30開演)</small></button>
            <button id="tabNight" class="perf-tab" onclick="switchPerformance('night')">🌙 夜公演<br><small>(18:00開演)</small></button>
        </div>

        <div class="stage">舞 台</div>

        <div class="legend">
            <div class="legend-item"><div class="legend-box" style="background:#2980b9;"></div>指定席 (F5〜L16)</div>
            <div class="legend-item"><div class="legend-box" style="background:#95a5a6; opacity:0.6;"></div>自由席エリア</div>
            <div class="legend-item"><div class="legend-box" style="background:#f39c12;"></div>選択中 / 仮抑え中</div>
            <div class="legend-item"><div class="legend-box" style="background:#e74c3c;"></div>予約済 / 予約不可</div>
        </div>

        <div class="theater-container">
            <div id="seatingChart" class="seating-chart"></div>
        </div>

        <div class="checkout">
            <div class="mode-switch">
                <button id="btnModeReserved" class="mode-btn active" onclick="setMode('reserved')">指定席を選択して予約</button>
                <button id="btnModeUnreserved" class="mode-btn" onclick="setMode('unreserved')">自由席を枚数購入</button>
            </div>

            <div id="formReserved">
                <h4 style="margin:5px 0;">選択指定席: <span id="selectedSeatNum" style="color:#d35400;">なし</span></h4>
                <div class="form-group">
                    <label>お名前（全角カタカナのみ）</label>
                    <input type="text" id="userNameReserved" placeholder="ヤマダ タロウ">
                </div>
                <div class="form-group">
                    <label>電話番号（半角数字・ハイフンなし）</label>
                    <input type="text" id="userPhoneReserved" placeholder="09012345678">
                </div>
                <div class="form-group">
                    <label>マイページ用パスワード</label>
                    <input type="password" id="userPasswordReserved" placeholder="任意のパスワードを設定">
                </div>
                <div class="form-group">
                    <label>お支払い・購入方法</label>
                    <select id="payMethodReserved" onchange="toggleMemberInput('reserved')">
                        <option value="stripe">クレジットカード決済 (Stripe)</option>
                        <option value="convenience">コンビニ決済 (Stripe)</option>
                        <option value="member">身内販売（団員経由・即確定）</option>
                    </select>
                </div>
                <div class="form-group" id="memberGroupReserved" style="display:none;">
                    <label style="color:#d35400;">団員ID（数字5桁）</label>
                    <input type="text" id="memberIdReserved" placeholder="例: 15212" maxlength="5">
                </div>
                <button class="submit-btn" onclick="openConfirmModal()">購入内容の確認へ進む</button>
            </div>

            <div id="formUnreserved" style="display:none;">
                <h4 style="margin:5px 0;">自由席購入</h4>
                <p style="font-size:12px; color:#666; margin:2px 0;">（残り自由席: <span id="unreservedCount">-</span> 席）</p>
                <div class="form-group">
                    <label>枚数選択</label>
                    <select id="unreservedQty">
                        <option value="1">1枚</option>
                        <option value="2">2枚</option>
                        <option value="3">3枚</option>
                        <option value="4">4枚</option>
                        <option value="5">5枚</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>代表者お名前（全角カタカナのみ）</label>
                    <input type="text" id="userNameUnreserved" placeholder="ヤマダ タロウ">
                </div>
                <div class="form-group">
                    <label>電話番号（半角数字・ハイフンなし）</label>
                    <input type="text" id="userPhoneUnreserved" placeholder="09012345678">
                </div>
                <div class="form-group">
                    <label>マイページ用パスワード</label>
                    <input type="password" id="userPasswordUnreserved" placeholder="任意のパスワードを設定">
                </div>
                <div class="form-group">
                    <label>お支払い・購入方法</label>
                    <select id="payMethodUnreserved" onchange="toggleMemberInput('unreserved')">
                        <option value="stripe">クレジットカード決済 (Stripe)</option>
                        <option value="convenience">コンビニ決済 (Stripe)</option>
                        <option value="member">身内販売（団員経由・即確定）</option>
                    </select>
                </div>
                <div class="form-group" id="memberGroupUnreserved" style="display:none;">
                    <label style="color:#d35400;">団員ID（数字5桁）</label>
                    <input type="text" id="memberIdUnreserved" placeholder="例: 15212" maxlength="5">
                </div>
                <button class="submit-btn" onclick="openConfirmModal()">購入内容の確認へ進む</button>
            </div>
        </div>
    </div>

    <!-- マイページ画面セクション -->
    <div id="sectionMypage" style="display:none;">
        <div class="mypage-box">
            <h3 style="margin-top:0; text-align:center;">マイページ（予約確認）</h3>
            <p style="font-size: 12px; color: #666; text-align: center;">ご購入時に登録した電話番号とパスワードを入力してください。</p>
            <div class="form-group">
                <label>電話番号（ハイフンなし）</label>
                <input type="text" id="loginPhone" placeholder="09012345678">
            </div>
            <div class="form-group">
                <label>パスワード</label>
                <input type="password" id="loginPassword" placeholder="パスワード">
            </div>
            <button class="submit-btn" onclick="fetchMypage()">予約情報を確認する</button>
        </div>
        <div id="mypageResult" style="max-width: 420px; margin: 10px auto;"></div>
    </div>

    <!-- フッターリンク -->
    <div class="footer-links">
        <a onclick="openTokushoModal()">特定商取引法に基づく表記</a>
    </div>

    <!-- 注文確認モーダル -->
    <div id="confirmModal" class="modal">
        <div class="modal-content">
            <h3 style="margin-top:0; text-align:center;">ご注文内容の確認</h3>
            <div id="confirmDetails" style="font-size:13px; margin-bottom:10px;"></div>
            
            <div class="invoice-box">
                <div class="invoice-row"><span>入場チケット (<span id="invTicketCount">0</span>枚)</span><span id="invTicketPrice">¥0</span></div>
                <div class="invoice-row"><span>決済手数料</span><span id="invFee">¥0</span></div>
                <div class="invoice-total"><span>お支払い合計金額</span><span id="invTotal">¥0</span></div>
            </div>

            <label style="font-size:12px; font-weight:bold;">【チケットご購入・ご来場に関する注意事項】</label>
            <div class="terms-box">■ チケットの取扱いについて
・本券は1枚につき1名様、指定の公演日時のみ有効です。
・本券の盗難、紛失、破損等による再発行は一切行いません。ご入場まで大切に保管してください。
・営利を目的としたチケットの転売・譲渡は固くお断りいたします。

■ 上演内容・演出に関するご注意（重要）
・本公演には、一部にハラスメント・暴力・火災・心的ストレスを想起させる描写およびショッキングな表現が含まれます。あらかじめご了承の上、ご鑑賞ください。
・演出の都合上、場内が暗転する場面や大きな音・光が出る演出がございます。体調が優れない場合は無理をせず、お近くのスタッフにお声がけください。

■ ご来場・開演中のお願い
・開演後のご入場は、演出の都合上、お席へのご案内までお待ちいただく場合や、ご希望のお席にご案内できない場合がございます。お時間には余裕を持ってお越しください。
・場内での許可のない写真撮影・録音・録画は固くお断りいたします。
・上演中の携帯電話・スマートフォン等は、あらかじめマナーモードに設定の上、音が鳴らないようお願いいたします。

■ キャンセル・返金について
・お客様のご都合による購入後のキャンセル・変更・払戻しは承れません。
・主催者側の都合により公演が中止となった場合のみ、所定の方法にて払戻しを行います。</div>

            <div class="agree-check">
                <input type="checkbox" id="agreeTerms" onchange="toggleSubmitBtn()">
                <label for="agreeTerms">注意事項・利用規約および特定商取引法に基づく表記に同意する</label>
            </div>

            <button id="finalSubmitBtn" class="submit-btn" style="background:#27ae60; opacity:0.5;" disabled onclick="executePurchase()">予約・決済手続きへ進む</button>
            <button onclick="closeConfirmModal()" style="margin-top:8px; background:#7f8c8d; color:white; border:none; padding:8px; border-radius:4px; font-size:12px; cursor:pointer; width:100%;">戻って修正する</button>
        </div>
    </div>

    <!-- 特定商取引法に基づく表記モーダル -->
    <div id="tokushoModal" class="modal">
        <div class="modal-content" style="max-width:500px;">
            <h3 style="margin-top:0; text-align:center;">特定商取引法に基づく表記</h3>
            <table class="tokusho-table">
                <tr>
                    <th>販売事業者名</th>
                    <td>虹凛プロジェクト</td>
                </tr>
                <tr>
                    <th>運営責任者</th>
                    <td>制作統括部門</td>
                </tr>
                <tr>
                    <th>お問い合わせ先</th>
                    <td>メール: project0106korin@gmail.com<br><small>※お電話でのお問い合わせが必要な場合はメールにてご請求ください。遅滞なく開示いたします。</small></td>
                </tr>
                <tr>
                    <th>販売価格</th>
                    <td>チケット単価：1,000円（税込）/ 枚</td>
                </tr>
                <tr>
                    <th>商品代金以外の必要料金</th>
                    <td>・クレジットカード決済手数料：100円 / 枚<br>・コンビニ決済手数料：220円 / 枚<br>・インターネット接続料金および通信費用は自己負担となります。</td>
                </tr>
                <tr>
                    <th>お支払い方法</th>
                    <td>・クレジットカード決済 (Visa, Mastercard, JCB, AMEX)<br>・コンビニ決済 (ローソン、ファミリーマート、ミニストップ、デイリーヤマザキ)<br>・身内販売（団員直接決済）</td>
                </tr>
                <tr>
                    <th>お支払い時期</th>
                    <td>・クレジットカード：即時決済（15分以内に完了しない場合は仮抑えが自動解除されます）<br>・コンビニ決済：ご注文完了後3日以内（72時間）に店頭にてお支払いください。期限を過ぎると自動キャンセルとなります。</td>
                </tr>
                <tr>
                    <th>引き渡し時期</th>
                    <td>決済完了後（カード即時またはコンビニ入金確認後）、マイページにて予約コードを即時発行いたします。（当日は受付にて予約コードをご提示いただくことでご入場いただけます）</td>
                </tr>
                <tr>
                    <th>キャンセル・返品（返金）について</th>
                    <td>お客様のご都合による購入確定後のキャンセル、日時変更、および払い戻しには対応いたしかねます。公演中止など主催者都合による変更の場合は、別途案内する手続きに沿って払い戻しを行います。</td>
                </tr>
            </table>
            <button onclick="closeTokushoModal()" style="margin-top:15px; width:100%; background:#007bff; color:white; border:none; padding:10px; border-radius:4px; font-weight:bold; cursor:pointer;">閉じる</button>
        </div>
    </div>

    <!-- 予約完了モーダル（身内販売用） -->
    <div id="successModal" class="modal">
        <div class="modal-content" style="text-align:center;">
            <h3>予約が完了しました！</h3>
            <p style="font-size: 13px; color: #555;">受付で使用しますので、以下の予約コードをお控えください。<br>（※「マイページ」からもいつでも確認できます）</p>
            <div id="modalCode" class="code-display">------</div>
            <button onclick="closeSuccessModal()" style="width:100%; background:#007bff; color:white; border:none; padding:10px; border-radius:4px; font-weight:bold; cursor:pointer;">マイページで確認する</button>
        </div>
    </div>

    <script>
        const TICKET_PRICE = 1000;
        const FEE_CONFIG = { 'stripe': 100, 'convenience': 220, 'member': 0 };
        const PAY_NAMES = { 'stripe': 'クレジットカード決済 (Stripe)', 'convenience': 'コンビニ決済 (Stripe)', 'member': '身内販売（団員経由）' };

        let currentPerformance = 'day';
        let selectedSeats = [];
        let currentMode = 'reserved';
        let pendingPurchaseData = null;

        window.addEventListener('DOMContentLoaded', () => {
            const params = new URLSearchParams(window.location.search);
            const status = params.get('status');
            const code = params.get('code');
            if (status === 'success' && code) {
                switchMainTab('mypage');
                showSuccessModal(code);
                window.history.replaceState({}, document.title, window.location.pathname);
            }
        });

        function switchMainTab(tab) {
            if (tab === 'booking') {
                document.getElementById('sectionBooking').style.display = 'block';
                document.getElementById('sectionMypage').style.display = 'none';
                document.getElementById('navBooking').classList.add('active');
                document.getElementById('navMypage').classList.remove('active');
            } else {
                document.getElementById('sectionBooking').style.display = 'none';
                document.getElementById('sectionMypage').style.display = 'block';
                document.getElementById('navMypage').classList.add('active');
                document.getElementById('navBooking').classList.remove('active');
            }
        }

        function switchPerformance(perf) {
            currentPerformance = perf;
            selectedSeats = [];
            updateSelectedSeatsDisplay();

            if (perf === 'day') {
                document.getElementById('tabDay').classList.add('active');
                document.getElementById('tabNight').classList.remove('active');
            } else {
                document.getElementById('tabNight').classList.add('active');
                document.getElementById('tabDay').classList.remove('active');
            }
            loadSeats();
        }

        function setMode(mode) {
            currentMode = mode;
            if(mode === 'reserved') {
                document.getElementById('btnModeReserved').classList.add('active');
                document.getElementById('btnModeUnreserved').classList.remove('active');
                document.getElementById('formReserved').style.display = 'block';
                document.getElementById('formUnreserved').style.display = 'none';
            } else {
                document.getElementById('btnModeUnreserved').classList.add('active');
                document.getElementById('btnModeReserved').classList.remove('active');
                document.getElementById('formReserved').style.display = 'none';
                document.getElementById('formUnreserved').style.display = 'block';
            }
        }

        function toggleMemberInput(type) {
            if(type === 'reserved') {
                const val = document.getElementById('payMethodReserved').value;
                document.getElementById('memberGroupReserved').style.display = (val === 'member') ? 'block' : 'none';
            } else {
                const val = document.getElementById('payMethodUnreserved').value;
                document.getElementById('memberGroupUnreserved').style.display = (val === 'member') ? 'block' : 'none';
            }
        }

        async function loadSeats() {
            const res = await fetch(`/api/seats?perf=${currentPerformance}`);
            const seats = await res.json();
            const seatsMap = {};
            let unreservedAvailableCount = 0;

            seats.forEach(s => {
                seatsMap[s.seat_number] = s;
                if(s.seat_type === 'unreserved' && s.status === 'available') {
                    unreservedAvailableCount++;
                }
            });

            document.getElementById('unreservedCount').innerText = unreservedAvailableCount;

            const chart = document.getElementById('seatingChart');
            chart.innerHTML = '';

            const rows = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O'];

            rows.forEach((r) => {
                if (r === 'K') {
                    const aisle = document.createElement('div');
                    aisle.className = 'aisle-v';
                    chart.appendChild(aisle);
                }

                const rowDiv = document.createElement('div');
                rowDiv.className = 'seat-row';

                const leftBlock = document.createElement('div');
                leftBlock.className = 'block-left';
                for (let i = 1; i <= 6; i++) leftBlock.appendChild(createSeatElement(`${r}-${i}`, i, seatsMap[`${r}-${i}`]));

                const aisle1 = document.createElement('div');
                aisle1.className = 'aisle-h';

                const label = document.createElement('div');
                label.className = 'row-label';
                label.innerText = r;

                const centerBlock = document.createElement('div');
                centerBlock.className = 'block-center';
                for (let i = 7; i <= 14; i++) centerBlock.appendChild(createSeatElement(`${r}-${i}`, i, seatsMap[`${r}-${i}`]));

                const aisle2 = document.createElement('div');
                aisle2.className = 'aisle-h';

                const rightBlock = document.createElement('div');
                rightBlock.className = 'block-right';
                for (let i = 15; i <= 20; i++) rightBlock.appendChild(createSeatElement(`${r}-${i}`, i, seatsMap[`${r}-${i}`]));

                rowDiv.appendChild(leftBlock);
                rowDiv.appendChild(aisle1);
                rowDiv.appendChild(label);
                rowDiv.appendChild(centerBlock);
                rowDiv.appendChild(aisle2);
                rowDiv.appendChild(rightBlock);

                chart.appendChild(rowDiv);
            });
        }

        function createSeatElement(seatNum, num, seatObj) {
            const div = document.createElement('div');
            const status = seatObj ? seatObj.status : 'available';
            const seatType = seatObj ? seatObj.seat_type : 'unreserved';

            let typeClass = seatType === 'reserved' ? 'reserved-seat' : 'unreserved-seat';
            const isSelected = selectedSeats.includes(seatNum);
            div.className = `seat ${typeClass} ${status} ${isSelected ? 'selected' : ''}`;
            div.innerText = num;

            if (seatType === 'reserved' && status === 'available') {
                div.onclick = () => toggleSeatSelection(seatNum, div);
            }
            return div;
        }

        function toggleSeatSelection(seatNum, el) {
            setMode('reserved');
            const index = selectedSeats.indexOf(seatNum);
            if (index > -1) {
                selectedSeats.splice(index, 1);
                el.classList.remove('selected');
            } else {
                selectedSeats.push(seatNum);
                el.classList.add('selected');
            }
            updateSelectedSeatsDisplay();
        }

        function updateSelectedSeatsDisplay() {
            const display = document.getElementById('selectedSeatNum');
            if (selectedSeats.length === 0) {
                display.innerText = 'なし';
            } else {
                display.innerText = `${selectedSeats.join(', ')} (計 ${selectedSeats.length} 席)`;
            }
        }

        function openConfirmModal() {
            let name, phone, password, payMethod, memberId, qty, seatTypeStr, seatsStr;

            if (currentMode === 'reserved') {
                if (selectedSeats.length === 0) return alert('指定席をマップから1つ以上選択してください');
                name = document.getElementById('userNameReserved').value.trim();
                phone = document.getElementById('userPhoneReserved').value.trim();
                password = document.getElementById('userPasswordReserved').value.trim();
                payMethod = document.getElementById('payMethodReserved').value;
                memberId = document.getElementById('memberIdReserved').value.trim();
                qty = selectedSeats.length;
                seatTypeStr = '指定席';
                seatsStr = selectedSeats.join(', ');
            } else {
                qty = parseInt(document.getElementById('unreservedQty').value);
                name = document.getElementById('userNameUnreserved').value.trim();
                phone = document.getElementById('userPhoneUnreserved').value.trim();
                password = document.getElementById('userPasswordUnreserved').value.trim();
                payMethod = document.getElementById('payMethodUnreserved').value;
                memberId = document.getElementById('memberIdUnreserved').value.trim();
                seatTypeStr = '自由席';
                seatsStr = `自由席 ${qty} 枚`;
            }

            if (!name) return alert('お名前を入力してください');
            if (!phone) return alert('電話番号を入力してください');
            if (!password) return alert('マイページ用パスワードを入力してください');
            if (payMethod === 'member' && !memberId) return alert('身内販売の場合は団員IDを入力してください');

            pendingPurchaseData = { performance_time: currentPerformance, mode: currentMode, name, phone, password, payMethod, memberId, qty, seat_numbers: selectedSeats };

            const ticketPriceSum = TICKET_PRICE * qty;
            const feeUnit = FEE_CONFIG[payMethod] || 0;
            const feeSum = feeUnit * qty;
            const totalSum = ticketPriceSum + feeSum;

            const perfText = (currentPerformance === 'day') ? '昼公演 (14:30開演)' : '夜公演 (18:00開演)';

            document.getElementById('confirmDetails').innerHTML = `
                <b>対象公演:</b> <span style="color:#d35400; font-weight:bold;">${perfText}</span><br>
                <b>お名前:</b> ${name}<br>
                <b>電話番号:</b> ${phone}<br>
                <b>座席種別:</b> ${seatTypeStr}<br>
                <b>座席情報:</b> ${seatsStr}<br>
                <b>お支払い方法:</b> ${PAY_NAMES[payMethod]} ${payMethod === 'member' ? '(団員ID: ' + memberId + ')' : ''}
            `;
            document.getElementById('invTicketCount').innerText = qty;
            document.getElementById('invTicketPrice').innerText = `¥${ticketPriceSum.toLocaleString()}`;
            document.getElementById('invFee').innerText = `¥${feeSum.toLocaleString()}`;
            document.getElementById('invTotal').innerText = `¥${totalSum.toLocaleString()}`;

            document.getElementById('agreeTerms').checked = false;
            toggleSubmitBtn();

            document.getElementById('confirmModal').style.display = 'block';
        }

        function toggleSubmitBtn() {
            const checked = document.getElementById('agreeTerms').checked;
            const btn = document.getElementById('finalSubmitBtn');
            btn.disabled = !checked;
            btn.style.opacity = checked ? '1.0' : '0.5';
        }

        function closeConfirmModal() {
            document.getElementById('confirmModal').style.display = 'none';
        }

        function openTokushoModal() {
            document.getElementById('tokushoModal').style.display = 'block';
        }

        function closeTokushoModal() {
            document.getElementById('tokushoModal').style.display = 'none';
        }

        async function executePurchase() {
            if (!pendingPurchaseData) return;

            const res = await fetch('/api/buy_reserved', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(pendingPurchaseData)
            });
            const data = await res.json();

            closeConfirmModal();

            if (data.success) {
                if (data.checkout_url) {
                    window.location.href = data.checkout_url;
                } else if (data.booking_code) {
                    showSuccessModal(data.booking_code);
                    selectedSeats = [];
                    updateSelectedSeatsDisplay();
                    loadSeats();
                } else {
                    alert('予約は完了しましたが、予約コードの取得に失敗しました。');
                }
            } else {
                alert('エラー: ' + data.message);
            }
        }

        function showSuccessModal(code) {
            document.getElementById('modalCode').innerText = code;
            document.getElementById('successModal').style.display = 'block';
        }

        function closeSuccessModal() {
            document.getElementById('successModal').style.display = 'none';
            switchMainTab('mypage');
            loadSeats();
        }

        async function fetchMypage() {
            const phone = document.getElementById('loginPhone').value.trim();
            const password = document.getElementById('loginPassword').value.trim();
            const resultDiv = document.getElementById('mypageResult');

            if (!phone || !password) {
                alert('電話番号とパスワードを入力してください');
                return;
            }

            resultDiv.innerHTML = '<p style="text-align:center; color:#666;">検索中...</p>';

            try {
                const res = await fetch('/api/mypage', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone, password })
                });
                const data = await res.json();

                if (data.success) {
                    if (data.tickets.length === 0) {
                        resultDiv.innerHTML = '<div class="mypage-box"><p style="text-align:center; color:#e74c3c; margin:0;">該当する予約情報が見つかりませんでした。<br><small>入力内容をご確認ください。</small></p></div>';
                        return;
                    }

                    let html = '<div class="mypage-box"><h4 style="margin-top:0; border-bottom:1px solid #eee; padding-bottom:6px;">ご予約一覧（お名前: ' + data.name + ' 様）</h4>';
                    data.tickets.forEach(t => {
                        const perfText = t.performance_time === 'day' ? '昼公演 (14:30開演)' : '夜公演 (18:00開演)';
                        const payText = PAY_NAMES[t.payment_method] || t.payment_method;
                        
                        let statusBadge = '';
                        let codeSection = '';

                        if (t.status === 'sold') {
                            statusBadge = '<span style="color:green; font-weight:bold;">購入・入金完了</span>';
                            codeSection = `
                                <div style="text-align:center; margin-top:8px;">
                                    <div style="font-size:11px; color:#555;">予約コード（受付で提示）</div>
                                    <div style="font-size:20px; font-weight:bold; color:#e74c3c; letter-spacing:2px;">${t.booking_code}</div>
                                </div>
                            `;
                        } else {
                            statusBadge = '<span style="color:orange; font-weight:bold;">仮抑え中（お支払い待ち）</span>';
                            codeSection = `
                                <div style="text-align:center; margin-top:8px; background:#fff3cd; padding:8px; border-radius:4px;">
                                    <div style="font-size:12px; color:#856404; font-weight:bold;">お支払い完了後にコードが発行されます</div>
                                    <div style="font-size:11px; color:#666; margin-top:2px;">カード決済またはコンビニ入金が完了すると、こちらに予約コードが表示されます。</div>
                                </div>
                            `;
                        }

                        html += `
                            <div class="ticket-card">
                                <b>対象公演:</b> ${perfText}<br>
                                <b>座席番号:</b> <span style="color:#d35400; font-weight:bold;">${t.seat_number}</span><br>
                                <b>ステータス:</b> ${statusBadge}<br>
                                <b>支払い方法:</b> ${payText}<br>
                                ${codeSection}
                            </div>
                        `;
                    });
                    html += '</div>';
                    resultDiv.innerHTML = html;
                } else {
                    resultDiv.innerHTML = `<div class="mypage-box"><p style="text-align:center; color:#e74c3c; margin:0;">${data.message}</p></div>`;
                }
            } catch (err) {
                resultDiv.innerHTML = '<div class="mypage-box"><p style="text-align:center; color:#e74c3c; margin:0;">通信エラーが発生しました。</p></div>';
            }
        }

        loadSeats();
    </script>
</body>
</html>
"""

# 管理画面のHTMLテンプレート
HTML_ADMIN = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>管理・受付照会画面 | 虹凛プロジェクト</title>
    <style>
        body { font-family: sans-serif; padding: 15px; background: #f9f9f9; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; background: white; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 13px; }
        th { background: #333; color: white; }
        .filter-container { display: flex; gap: 10px; margin-bottom: 10px; align-items: center; flex-wrap: wrap; }
        input, select { padding: 10px; font-size: 14px; box-sizing: border-box; border-radius: 4px; border: 1px solid #ccc; }
        .badge-res { background: #2980b9; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px; }
        .badge-unres { background: #7f8c8d; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px; }
        .badge-day { background: #e67e22; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: bold; }
        .badge-night { background: #2c3e50; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: bold; }
        .code-tag { font-weight: bold; color: #d35400; font-family: monospace; font-size: 14px; }
        .pay-tag { font-size: 11px; padding: 2px 5px; border-radius: 3px; background: #eee; }
    </style>
</head>
<body>
    <h1>受付・照会（管理画面）</h1>
    
    <div class="filter-container">
        <label style="font-weight:bold; font-size:14px;">公演絞り込み:</label>
        <select id="perfFilter" onchange="loadAdminData()">
            <option value="day">☀️ 昼公演 (14:30) のみ表示</option>
            <option value="night">🌙 夜公演 (18:00) のみ表示</option>
            <option value="all">全公演（昼・夜）を表示</option>
        </select>
        <input type="text" id="searchInput" onkeyup="filterTable()" placeholder="団員ID、予約コード、名前、電話番号、座席番号で検索..." style="flex:1; max-width:400px;">
    </div>
    
    <table id="seatTable">
        <thead>
            <tr>
                <th>公演</th>
                <th>座席番号</th>
                <th>種別</th>
                <th>状態</th>
                <th>カタカナ名</th>
                <th>電話番号</th>
                <th>決済方法</th>
                <th>団員ID</th>
                <th>予約コード</th>
                <th>支払期限（仮抑え期限）</th>
            </tr>
        </thead>
        <tbody id="tableBody"></tbody>
    </table>

    <script>
        async function loadAdminData() {
            const perf = document.getElementById('perfFilter').value;
            const res = await fetch(`/api/seats?perf=${perf}`);
            const seats = await res.json();
            const tbody = document.getElementById('tableBody');
            tbody.innerHTML = '';
            
            const payLabels = { 'stripe': 'カード決済', 'convenience': 'コンビニ決済', 'member': '身内販売' };

            seats.forEach(s => {
                const tr = document.createElement('tr');
                const perfBadge = s.performance_time === 'day' ? '<span class="badge-day">昼公演</span>' : '<span class="badge-night">夜公演</span>';
                const typeBadge = s.seat_type === 'reserved' ? '<span class="badge-res">指定席</span>' : '<span class="badge-unres">自由席</span>';
                
                let statusStr = '空席';
                if (s.status === 'sold') {
                    statusStr = '<span style="color:green;font-weight:bold;">購入・入金完了</span>';
                } else if (s.status === 'pending_payment') {
                    statusStr = '<span style="color:orange;font-weight:bold;">仮抑え中</span>';
                }

                const codeStr = (s.status === 'sold' && s.booking_code) ? `<span class="code-tag">${s.booking_code}</span>` : '-';
                const payStr = s.payment_method ? `<span class="pay-tag">${payLabels[s.payment_method] || s.payment_method}</span>` : '-';
                const memberStr = s.member_id ? `<b>${s.member_id}</b>` : '-';
                const expiresStr = s.expires_at ? s.expires_at : '-';

                tr.innerHTML = `<td>${perfBadge}</td><td>${s.seat_number}</td><td>${typeBadge}</td><td>${statusStr}</td><td>${s.purchased_by || '-'}</td><td>${s.phone || '-'}</td><td>${payStr}</td><td>${memberStr}</td><td>${codeStr}</td><td><small>${expiresStr}</small></td>`;
                tbody.appendChild(tr);
            });
            filterTable();
        }

        function filterTable() {
            const filter = document.getElementById('searchInput').value.toUpperCase().trim();
            const rows = document.getElementById('tableBody').getElementsByTagName('tr');
            for (let i = 0; i < rows.length; i++) {
                const txt = rows[i].textContent || rows[i].innerText;
                rows[i].style.display = txt.toUpperCase().indexOf(filter) > -1 ? "" : "none";
            }
        }

        loadAdminData();
    </script>
</body>
</html>
"""

# ---------------------------------------------------------
# ルート定義
# ---------------------------------------------------------
@app.route('/')
def index():
    release_expired_seats()
    return render_template_string(HTML_BUY)

@app.route('/admin')
def admin():
    release_expired_seats()
    return render_template_string(HTML_ADMIN)

@app.route('/api/seats', methods=['GET'])
def api_seats():
    release_expired_seats()
    perf = request.args.get('perf', 'day')
    
    conn = get_db_connection()
    c = conn.cursor()
    
    if perf == 'all':
        if DATABASE_URL:
            c.execute('SELECT performance_time, seat_number, seat_type, status, purchased_by, phone, payment_method, member_id, booking_code, expires_at FROM seats')
        else:
            c.execute('SELECT performance_time, seat_number, seat_type, status, purchased_by, phone, payment_method, member_id, booking_code, expires_at FROM seats')
    else:
        if DATABASE_URL:
            c.execute('SELECT performance_time, seat_number, seat_type, status, purchased_by, phone, payment_method, member_id, booking_code, expires_at FROM seats WHERE performance_time = %s', (perf,))
        else:
            c.execute('SELECT performance_time, seat_number, seat_type, status, purchased_by, phone, payment_method, member_id, booking_code, expires_at FROM seats WHERE performance_time = ?', (perf,))
            
    rows = c.fetchall()
    conn.close()
    
    seats_list = []
    for r in rows:
        seats_list.append({
            'performance_time': r[0],
            'seat_number': r[1],
            'seat_type': r[2],
            'status': r[3],
            'purchased_by': r[4],
            'phone': r[5],
            'payment_method': r[6],
            'member_id': r[7],
            'booking_code': r[8],
            'expires_at': r[9]
        })
        
    return jsonify(seats_list)

@app.route('/api/buy_reserved', methods=['POST'])
def api_buy_reserved():
    data = request.json
    perf = data.get('performance_time')
    name = data.get('name')
    
    phone = normalize_phone(data.get('phone', ''))
    password = data.get('password', '').strip()
    pay_method = data.get('payMethod')
    member_id = data.get('memberId', '')
    mode = data.get('mode', 'reserved')
    
    if not name or not phone or not password:
        return jsonify({'success': False, 'message': '入力内容が不足しています。'})
        
    if not is_katakana(name):
        return jsonify({'success': False, 'message': 'お名前は全角カタカナで入力してください。'})
        
    if not is_phone_number(phone):
        return jsonify({'success': False, 'message': '電話番号はハイフンなしの半角数字10〜11桁で正しく入力してください（例: 09012345678）。'})
        
    if pay_method == 'member' and member_id not in VALID_MEMBER_IDS:
        return jsonify({'success': False, 'message': '団員IDが無効です。'})
        
    conn = get_db_connection()
    c = conn.cursor()
    
    target_seats = []
    
    if mode == 'reserved':
        seat_numbers = data.get('seat_numbers', [])
        if not seat_numbers:
            return jsonify({'success': False, 'message': '指定席が選択されていません。'})
            
        for s_num in seat_numbers:
            if DATABASE_URL:
                c.execute('SELECT status FROM seats WHERE performance_time = %s AND seat_number = %s', (perf, s_num))
            else:
                c.execute('SELECT status FROM seats WHERE performance_time = ? AND seat_number = ?', (perf, s_num))
            row = c.fetchone()
            if not row or row[0] != 'available':
                conn.close()
                return jsonify({'success': False, 'message': f'座席 {s_num} はすでに埋まっています。'})
        target_seats = seat_numbers
    else:
        qty = int(data.get('qty', 1))
        if DATABASE_URL:
            c.execute('SELECT seat_number FROM seats WHERE performance_time = %s AND seat_type = %s AND status = %s LIMIT %s', (perf, 'unreserved', 'available', qty))
        else:
            c.execute('SELECT seat_number FROM seats WHERE performance_time = ? AND seat_type = ? AND status = ? LIMIT ?', (perf, 'unreserved', 'available', qty))
        rows = c.fetchall()
        
        if len(rows) < qty:
            conn.close()
            return jsonify({'success': False, 'message': '申し訳ありません。ご希望の枚数の自由席が残っていません。'})
            
        target_seats = [r[0] for r in rows]
            
    booking_code = generate_booking_code()
    
    if pay_method == 'member':
        status = 'sold'
        expires_at = None
        for s_num in target_seats:
            if DATABASE_URL:
                c.execute('''UPDATE seats SET status = %s, purchased_by = %s, phone = %s, password = %s, booking_code = %s, payment_method = %s, member_id = %s, expires_at = %s 
                            WHERE performance_time = %s AND seat_number = %s''',
                          (status, name, phone, password, booking_code, pay_method, member_id, expires_at, perf, s_num))
            else:
                c.execute('''UPDATE seats SET status = ?, purchased_by = ?, phone = ?, password = ?, booking_code = ?, payment_method = ?, member_id = ?, expires_at = ? 
                            WHERE performance_time = ? AND seat_number = ?''',
                          (status, name, phone, password, booking_code, pay_method, member_id, expires_at, perf, s_num))
        conn.commit()
        conn.close()
        
        return jsonify({'success': True, 'booking_code': booking_code})
    else:
        status = 'pending_payment'
        if pay_method == 'convenience':
            expires_at = (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
        else:
            expires_at = (datetime.now() + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')

        for s_num in target_seats:
            if DATABASE_URL:
                c.execute('''UPDATE seats SET status = %s, purchased_by = %s, phone = %s, password = %s, booking_code = %s, payment_method = %s, member_id = %s, expires_at = %s 
                            WHERE performance_time = %s AND seat_number = %s''',
                          (status, name, phone, password, booking_code, pay_method, member_id, expires_at, perf, s_num))
            else:
                c.execute('''UPDATE seats SET status = ?, purchased_by = ?, phone = ?, password = ?, booking_code = ?, payment_method = ?, member_id = ?, expires_at = ? 
                            WHERE performance_time = ? AND seat_number = ?''',
                          (status, name, phone, password, booking_code, pay_method, member_id, expires_at, perf, s_num))
        conn.commit()
        conn.close()
        
        try:
            qty = len(target_seats)
            unit_price = TICKET_PRICE + FEE_CONFIG.get(pay_method, 0)
            
            host_url = request.host_url.rstrip('/')
            
            checkout_session = stripe.checkout.Session.create(
                line_items=[{
                    'price_data': {
                        'currency': 'jpy',
                        'product_data': {'name': f'【虹凛プロジェクト】チケット ({", ".join(target_seats)})'},
                        'unit_amount': unit_price,
                    },
                    'quantity': qty,
                }],
                mode='payment',
                success_url=f'{host_url}/api/stripe_success?code={booking_code}',
                cancel_url=f'{host_url}/',
            )
            return jsonify({'success': True, 'checkout_url': checkout_session.url})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})

@app.route('/api/stripe_success', methods=['GET'])
def stripe_success():
    booking_code = request.args.get('code')
    if booking_code:
        conn = get_db_connection()
        c = conn.cursor()
        
        if DATABASE_URL:
            c.execute('SELECT booking_code, payment_method FROM seats WHERE booking_code = %s', (booking_code,))
        else:
            c.execute('SELECT booking_code, payment_method FROM seats WHERE booking_code = ?', (booking_code,))
        row = c.fetchone()
        
        if row:
            pay_method = row[1]
            if pay_method == 'stripe':
                if DATABASE_URL:
                    c.execute('UPDATE seats SET status = %s WHERE booking_code = %s', ('sold', booking_code))
                else:
                    c.execute('UPDATE seats SET status = ? WHERE booking_code = ?', ('sold', booking_code))
                conn.commit()
            
            conn.close()
            return redirect(url_for('index') + '?status=success&code=' + booking_code)
            
        conn.close()
    return redirect(url_for('index'))

@app.route('/api/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError:
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
    return jsonify({'status': 'success'}), 200

@app.route('/api/mypage', methods=['POST'])
def api_mypage():
    data = request.json
    phone = normalize_phone(data.get('phone', ''))
    password = data.get('password', '').strip()
    
    if not phone or not password:
        return jsonify({'success': False, 'message': '電話番号とパスワードを入力してください。'})
        
    conn = get_db_connection()
    c = conn.cursor()
    
    if DATABASE_URL:
        c.execute('SELECT performance_time, seat_number, status, payment_method, booking_code, purchased_by FROM seats WHERE phone = %s AND password = %s', (phone, password))
    else:
        c.execute('SELECT performance_time, seat_number, status, payment_method, booking_code, password, purchased_by FROM seats WHERE phone = ? AND password = ?', (phone, password))
    rows = c.fetchall()
    conn.close()
    
    tickets = []
    name = ""
    for r in rows:
        name = r[5]
        tickets.append({
            'performance_time': r[0],
            'seat_number': r[1],
            'status': r[2],
            'payment_method': r[3],
            'booking_code': r[4]
        })
        
    return jsonify({'success': True, 'name': name, 'tickets': tickets})

if __name__ == '__main__':
    app.run(debug=True, port=5500)
