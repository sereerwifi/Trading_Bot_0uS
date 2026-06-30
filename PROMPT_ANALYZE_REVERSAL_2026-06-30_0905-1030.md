# Prompt สำหรับ Claude Code — วิเคราะห์การกลับตัวของกราฟ 30 มิ.ย. 2026 09:05–10:30

วาง prompt นี้ใน Claude Code **ที่โฟลเดอร์ของบอทที่มีข้อมูล log/state จริงของช่วงเวลานี้**
(ถ้าบอทรันอยู่บน VPS ให้รันที่ VPS — ไฟล์ log และ snapshot ของเวลาจริงจะอยู่ที่นั่น
ไม่ใช่ในโฟลเดอร์ sandbox นี้ ซึ่งไม่มี live runtime state ของวันนี้)

---

ช่วยเช็คกราฟ XAUUSD ในช่วงเวลา **30 มิถุนายน 2026, 09:05–10:30 (เวลาในเครื่อง/Thailand
time ตามที่ `data["now"]` ใช้)** ซึ่งราคามีการกลับตัว (reversal) เกิดขึ้น
ให้ตรวจสอบอย่างละเอียดว่า ณ ช่วงเวลานั้น มี strategy/สัญญาณใดบ้าง (จากทั้งหมด 33
strategies ที่บอทมี) ที่ให้สัญญาณ (vote long/short หรือ note ที่น่าสนใจ) บ้าง
และประเมินว่าสัญญาณเหล่านั้นน่าเชื่อถือ/ใช้เป็น indicator ในการตัดสินใจเทรดได้จริงหรือไม่
โดยทำตามขั้นตอนนี้:

## 1. ดึงข้อมูลราคาจริงในช่วงเวลานั้นก่อน

- อ่าน `xauusd_mt5_strategy.log` กรองเฉพาะ timestamp ระหว่าง 09:00–10:35 ของวันที่
  30 มิ.ย. 2026 เพื่อดู: ราคาที่บอทเห็นในแต่ละ scan, ผล daily filter/group bias,
  error หรือ veto ใดๆ
- ถ้ามี `price_bars` table ใน `fib_confluence_history.db` (จาก
  `fib_confluence.get_price_bars()`) หรือ historical bars อื่นที่บอทบันทึกไว้เอง
  (M1/M5/M15/H1) ให้ query ช่วงเวลานี้มาดู OHLC จริงเพื่อยืนยันรูปแบบแท่งเทียนที่
  ทำให้เกิดการกลับตัว (เช่น pin bar, engulfing, climax move แล้ว reverse)

## 2. เช็คทุก strategy ที่มีโอกาสให้สัญญาณช่วงนั้น

ไล่ดู snapshot/log ของแต่ละ strategy ในช่วง 09:05–10:30 โดยเฉพาะ:

- `strategy_scores.json` ถ้ามี snapshot history ย้อนหลัง (หรือ log บรรทัดที่ print
  ผล scoring ของแต่ละ scan) — ดู `logic_groups`/strategy notes ของทุก strategy ว่า
  long/short score เท่าไหร่ note พูดว่าอะไร
- `fib_confluence_history.db` → table `fib_confluence_history` — ดูว่าช่วงนั้นมี
  confluence zone (support/resistance) อยู่ใกล้ราคาหรือไม่ และมี
  `score_fib_confluence_sr` vote หรือไม่
- `harmonic_patterns_history.db` → table `harmonic_pattern_history` — ดูว่าช่วงนั้น
  XABCD pattern ใดถูก match (Gartley/Bat/Butterfly/Crab/Deep Crab/Cypher), PRZ
  ราคาเท่าไหร่, `fib_aligned` เป็น true หรือไม่ (สอดคล้องกับ Fibonacci strategy #32
  หรือไม่)
- strategy อื่นที่เกี่ยวกับ reversal/climax โดยตรง: `climax_reversal_sr` (#26),
  `zone_mw_reversal` (#29), `mtr_range_regime`/`mtr_trend_regime` (#27-28),
  `smart_money_sweep_morning` (#30 — ช่วงเวลานี้ตรงกับ session ของ strategy นี้พอดี
  เพราะ window คือ ~07:00-10:00 Thai time ให้เช็คเป็นพิเศษ), และ macro/Myfxbook
  sentiment ถ้ามีข้อมูล
- ถ้าไม่มี log/snapshot ละเอียดพอสำหรับช่วงเวลานี้ (เช่นบอทไม่ได้รันช่วงนั้น หรือ
  `ENTRY_MODE`/strategy บางตัวไม่ได้ enable) ให้รายงานตรงๆว่าข้อมูลไม่พอที่จะสรุป
  อย่าเดา

## 3. วิเคราะห์อย่างละเอียด

สำหรับทุก strategy ที่พบว่าให้สัญญาณ (หรือใกล้จะให้สัญญาณ) ในช่วงเวลานี้ ให้สรุป:

- ชื่อ strategy, ทิศทางที่ vote (long/short), score, และ note/เหตุผลที่ scoring
  ให้คะแนนแบบนั้น
- ราคาที่สัญญาณเกิดขึ้น เทียบกับราคาที่กลับตัวจริง (สัญญาณมาก่อน/ตรง/หลังจุดกลับตัว
  กี่ pip/นาที)
- ถ้ามีหลาย strategy ให้สัญญาณพร้อมกันหรือใกล้กัน (confluence) ให้ชี้ว่าตัวไหน
  สอดคล้องกัน เช่น harmonic PRZ ตรงกับ fib confluence zone ตรงกับ climax reversal
  candle หรือไม่
- ประเมินความน่าเชื่อถือ: ratio ของ confirmation ที่มี (เช่น harmonic
  `n_confirm`/`confluence_score`, fib `confirmations` list), และเทียบกับว่าผลลัพธ์
  จริง (ราคากลับตัวไปเท่าไหร่ ไปทางไหน) ตรงกับสัญญาณที่ดีที่สุดหรือไม่
- สรุปว่า ถ้าจะใช้ event นี้เป็นเคสตัวอย่างเพื่อปรับ weight/threshold ของ strategy
  ใด ควรปรับอะไร (แต่**ห้ามแก้ weight/threshold หรือพารามิเตอร์ความเสี่ยงใดๆ
  ในรอบนี้** — แค่วิเคราะห์และเสนอแนะ ตามกฎของโปรเจกต์ที่ต้องให้ user confirm ก่อน
  ทุกครั้ง)

## 4. ส่งมอบผลลัพธ์

สรุปเป็นรายงานที่มี:

- timeline สั้นๆของราคา/แท่งเทียนสำคัญในช่วง 09:05–10:30
- ตาราง/รายการ strategy ที่ให้สัญญาณ พร้อมรายละเอียดข้อ 3
- ข้อสรุปว่า "สัญญาณไหนน่าเชื่อถือที่สุดสำหรับเคสนี้ และเพราะอะไร"
- ถ้าไม่พบสัญญาณจาก strategy ใดเลยในช่วงเวลานี้ (เช่นเพราะ entry mode/threshold
  filter ออกไปหมด) ให้บอกตรงๆว่าทำไมบอทถึง "เงียบ" ในช่วงที่ราคากลับตัวจริง — นี่คือ
  คำตอบที่มีค่าพอกับการเจอสัญญาณที่ตรง เพราะมันจะชี้ว่า threshold/filter ตัวไหน
  block สัญญาณที่ควรจะมา

อย่าแก้ไขโค้ด/config ใดๆในรอบนี้ — งานนี้คือการวิเคราะห์/รายงานเท่านั้น
