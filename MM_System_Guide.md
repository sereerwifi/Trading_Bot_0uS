# คู่มือระบบ Money Management (MM) — XAUUSD MT5 EA

## 1. สรุประบบ MM ทั้งหมดที่มีอยู่ตอนนี้

ทุกฟีเจอร์อยู่ใน `xauusd_mt5_strategy.py` และตั้งค่าได้จาก `strategy_config_ui.py` (เขียนลง `strategy_config.json`)

### 1.1 Fixed Fractional Position Sizing
`calc_lot_size()` คำนวณ Lot จาก % ความเสี่ยงของ **เงินทุนที่ปิดแล้ว** (`balance`) ไม่ใช่ equity เสมอ — ผ่านการทดสอบแล้วว่าให้ผลตรงกับสูตรมาตรฐาน: เงินทุน $10,000 เสี่ยง 2% ($200) เข้า 2300 SL 2295 (ระยะ $5 = 500 points) ได้ Lot 0.4 ตรงตามตัวอย่างที่ใช้กันทั่วไป จากนั้นค่าที่ได้จะถูก clamp ระหว่าง `MIN_LOT`/`MAX_LOT`

ตั้งค่าหลัก: `RISK_PER_TRADE` (ค่าเริ่มต้นในไฟล์ config ปัจจุบัน: ดูหัวข้อ 1.7), `MIN_LOT`, `MAX_LOT`, `ENFORCE_MIN_LOT`

### 1.2 R:R Floor (Risk-to-Reward Gate)
`passes_risk_reward()` เช็คทุกสัญญาณก่อนส่งออเดอร์ — ถ้า TP2:SL ต่ำกว่า `MIN_RISK_REWARD_RATIO` (ค่าเริ่มต้น 1.5 = R:R 1:1.5) ระบบจะ**ปัดสัญญาณนั้นทิ้งทันที** ไม่ว่าเทคนิคอลจะสวยแค่ไหน ทดสอบแล้วว่าทำงานถูกต้องทั้งกรณีผ่านและกรณีถูกปัด

### 1.3 Anti-Martingale — Consecutive Loss Breaker
`check_consecutive_loss_breaker()` นับจำนวนแพ้ติดกัน (จากประวัติดีลที่ปิดวันนี้) ถ้าแพ้ครบ `MAX_CONSECUTIVE_LOSSES` ครั้งติดกัน (ค่าเริ่มต้น 3) ระบบจะ**หยุดเปิดออเดอร์ใหม่ทั้งวันทันที** — ทดสอบแล้วว่านับ streak ถูกต้องและ reset เมื่อมีไม้ชนะ

### 1.4 Anti-Martingale by Construction
เพราะ `calc_lot_size()` ใช้ `balance` (เงินที่ปิดจริงแล้ว) เสมอ ไม่ใช่ equity ลอยตัว → Lot จะโตได้ก็ต่อเมื่อพอร์ตปิดกำไรจริงแล้วเท่านั้น เป็นไปไม่ได้ที่ระบบจะเพิ่ม Lot เพื่อ "แก้แค้น" หลังแพ้ (กับดัก Martingale)

### 1.5 Drawdown Breaker
`check_drawdown_breaker()` เทียบ equity ปัจจุบันกับจุดสูงสุด (peak) ของ session — ถ้า drawdown ถึง `MAX_DRAWDOWN_PCT` (ค่าเริ่มต้นในไฟล์ config: 5%) จะหยุดเปิดออเดอร์ใหม่จนกว่า equity จะฟื้น

### 1.6 Daily Loss Limit / Max Daily Trades
- `check_daily_loss_limit()`: หยุดเทรดวันนั้นทันทีถ้า P&L รวม (realized+floating) แตะ `-(DAILY_LOSS_LIMIT_R × risk_amount)`
- `MAX_DAILY_TRADES`: จำกัดจำนวนไม้เปิดใหม่ต่อวัน (ค่าปัจจุบันในไฟล์ config: 3 ไม้/วัน)

### 1.7 ช่วงเวลาเทรด (Trading Hours Filter) — ฟีเจอร์ใหม่
`is_within_trading_hours()` อนุญาตเปิดออเดอร์ใหม่เฉพาะช่วงเวลาที่เลือกไว้ (เวลาท้องถิ่นของเครื่อง ต้องตั้งเป็นเวลาไทย UTC+7):

| ช่วง | เวลา | ลักษณะ |
|---|---|---|
| Asia | 07:00–12:00 | เงียบ/Sideways |
| London | 14:00–17:00 | คึกคัก/Breakout |
| Overlap ⭐ | 19:00–23:00 | Golden Period วิ่งแรงที่สุด |

เลือกได้จากแท็บ **"ช่วงเวลาเทรด"** ใน `strategy_config_ui.py` (เลือกได้มากกว่า 1 ช่วง) — ค่าเริ่มต้นปัจจุบัน: เปิดใช้ตัวกรอง และเลือกเฉพาะช่วง Overlap

ตำแหน่งของกฎนี้ในขั้นตอนการเช็ค: เป็นเกตแรกสุดใน `run_once()` ก่อนเช็คเงื่อนไข MM อื่นๆ ทั้งหมด — ถ้าอยู่นอกช่วงเวลาที่เลือก จะไม่เสียเวลาคำนวณอย่างอื่นเลย โพสิชั่นที่เปิดอยู่แล้วยังถูก trailing stop ดูแลตามปกติไม่ว่าจะอยู่ในช่วงเวลาหรือไม่

### ค่าปัจจุบันในไฟล์ `strategy_config.json` (ที่ตรวจสอบแล้ว โหลดเข้าตัวแปรถูกต้อง 100%)
```
risk_per_trade: ใช้ค่า default ในสคริปต์ (RISK_PER_TRADE = 1%) เพราะไฟล์ config ปัจจุบันยังไม่มี section "risk"
min_lot: 0.1   max_lot: 2.0   enforce_min_lot: false
max_daily_trades: 3   max_drawdown_pct: 5.0%   daily_loss_limit_r: (ปิดใช้งาน)
min_risk_reward_ratio: 1.5   max_consecutive_losses: 3
trading_hours: เปิดใช้งาน — เลือกเฉพาะช่วง Overlap (19:00-23:00)
```
⚠️ หมายเหตุ: ไฟล์ `strategy_config.json` ปัจจุบันมีแค่ section `money_management` และ `trading_hours` — ยังไม่มี section `strategies`/`trailing_stop`/`risk`/`daily_filter` ทำให้ค่าพวกนั้นใช้ default ในสคริปต์ไปก่อน แนะนำให้เปิด `strategy_config_ui.py` แล้วกด **Save Config** สักครั้งเพื่อให้ไฟล์สมบูรณ์ครบทุก section (ดูขั้นตอนที่ 3 ด้านล่าง)

---

## 2. ผลการตรวจสอบโค้ด (verification)

รันแล้วผ่านทั้งหมด:
- `py_compile` ทั้ง 2 ไฟล์ — ไม่มี syntax error
- `strategy_config.json` — เป็น JSON ที่ถูกต้อง โหลดเข้าตัวแปรในสคริปต์ได้ครบ
- Unit test (จำลอง MetaTrader5 module เพื่อทดสอบ logic โดยไม่ต้องมี MT5 จริง):
  - `calc_lot_size` ตรงกับตัวอย่างคำนวณมือ (0.4 lot), clamp MIN/MAX ถูกต้อง, ระยะ SL=0 ได้ 0 lot
  - `passes_risk_reward` ผ่าน/ไม่ผ่านถูกต้องตามอัตราส่วนจริง และปิดใช้งานได้เมื่อตั้งเป็น None
  - `count_consecutive_losses` / `check_consecutive_loss_breaker` นับ streak แพ้ติดกันถูกต้อง และ reset เมื่อมีไม้ชนะ
  - `is_within_trading_hours` ตรงตามช่วงเวลาทั้ง 3 ช่วง และปิดตัวกรองได้ถูกต้อง
- พบและแก้บั๊กเดิมใน `strategy_config_ui.py`: ฟิลด์ซ้อน (เช่น `ema_cross.fast`, และฟิลด์ `sessions.asia` ที่เพิ่มใหม่) ใช้การค้นหาแบบ flat key ทำให้ UI พังตอนเปิด — แก้เป็น nested lookup แล้ว ทดสอบ import ผ่าน

สิ่งที่ทดสอบในสภาพแวดล้อมนี้ไม่ได้ (ต้องทดสอบบนเครื่องคุณเอง): การเชื่อมต่อ MT5 จริง, การส่งออเดอร์จริง, และการเปิดหน้า Tkinter UI จริง (เพราะ sandbox นี้ไม่มี MetaTrader5 terminal และไม่มีจอแสดงผล GUI)

---

## 3. ขั้นตอนติดตั้งและรันบนเครื่องคุณ (Windows + MetaTrader 5)

### ขั้นตอนที่ 1 — เตรียมเครื่อง
1. ติดตั้ง MetaTrader 5 จากโบรกเกอร์ของคุณ และ**ล็อกอินเข้าบัญชีให้เรียบร้อย** (แนะนำใช้บัญชี Demo ก่อนเสมอ) เปิดโปรแกรม MT5 ทิ้งไว้
2. ติดตั้ง Python 3.10+ จาก python.org (ติ๊ก "Add Python to PATH" ตอนติดตั้ง)
3. เปิด Command Prompt ไปที่โฟลเดอร์โปรเจกต์ แล้วติดตั้งไลบรารีที่ต้องใช้:
   ```
   pip install MetaTrader5 pandas numpy
   ```

### ขั้นตอนที่ 2 — ตั้งค่าเวลาเครื่อง
ตั้งนาฬิกาของเครื่อง (Windows time zone) เป็น **เวลาไทย (UTC+7)** เพราะตัวกรองช่วงเวลาเทรดใช้เวลานี้ในการตัดสินใจ

### ขั้นตอนที่ 3 — เปิดหน้าตั้งค่า (Config UI)
```
python strategy_config_ui.py
```
- แท็บ **"เงื่อนไขเข้าออเดอร์"**: เลือกกลยุทธ์ (ตอนนี้มีแค่ Fibonacci Retracement + Confluence ที่ใช้งานจริงในตัวสคริปต์)
- แท็บ **"ช่วงเวลาเทรด"**: เลือกช่วงเวลาที่ต้องการเทรด (แนะนำเริ่มจาก Overlap 19:00-23:00 เพราะวิ่งแรงสุดและชัดสุด)
- แท็บ **"Risk & Basket Close"**: ตั้ง % ความเสี่ยงต่อไม้ (`risk_per_trade_pct`), Min/Max Lot, R:R ขั้นต่ำ, จำนวนแพ้ติดกันที่ให้หยุด
- กด **Save Config** — จะเขียนค่าทั้งหมดลง `strategy_config.json` ให้ครบทุก section

### ขั้นตอนที่ 4 — ทดสอบแบบ Dry Run (ไม่ส่งออเดอร์จริง)
เปิดไฟล์ `xauusd_mt5_strategy.py` เช็คว่า `AUTO_TRADE = False` (ค่าเริ่มต้นเป็นแบบนี้อยู่แล้ว) จากนั้นรัน:
```
python xauusd_mt5_strategy.py
```
สคริปต์จะเชื่อมต่อ MT5, อ่านค่า config, แล้ววนเช็คสัญญาณ — มันจะ**พิมพ์สัญญาณ/Lot ที่จะใช้ออกมาให้ดูเฉยๆ ไม่ส่งออเดอร์จริง** ปล่อยให้รันแบบนี้สักหลายวันบนบัญชี Demo เพื่อดูว่าสัญญาณที่ออกมาสมเหตุสมผลหรือไม่

### ขั้นตอนที่ 5 — เปิดใช้งานจริง (หลังพอใจผลทดสอบบน Demo แล้วเท่านั้น)
แก้ `AUTO_TRADE = True` ใน `xauusd_mt5_strategy.py` แล้วรันสคริปต์ใหม่ — **แนะนำให้ทดสอบบนบัญชี Demo ก่อนใช้เงินจริงเสมอ** และเริ่มด้วย Lot/ความเสี่ยงที่เล็กที่สุดก่อน

### ขั้อสำคัญที่ต้องระวัง
- ต้องเปิดโปรแกรม MT5 ทิ้งไว้ และสคริปต์ต้องรันต่อเนื่อง (loop) ไม่ปิดหน้าต่าง ไม่งั้น Trailing Stop จะหยุดขยับ
- Anti-Martingale breaker / Daily loss limit / Drawdown breaker จะหยุด "เปิดไม้ใหม่" เท่านั้น ไม้ที่เปิดอยู่แล้วจะยังถูกดูแล trailing stop ตามปกติ
- การตั้ง `RISK_PER_TRADE` ไม่ควรเกิน 2% ต่อไม้สำหรับทองคำ ตามกฎเหล็กที่ตกลงกันไว้
