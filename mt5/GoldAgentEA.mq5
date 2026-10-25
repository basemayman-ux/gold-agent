//+------------------------------------------------------------------+
//| GoldAgentEA.mq5 - trades the gold-agent signals on an MT5 DEMO   |
//| Reads docs/data/signal.json from GitHub, opens ONE market order  |
//| per signal: stop = 1R, target = 1R, stop moves to entry at 0.7R. |
//| Prices are the broker's own: only the signal's DISTANCE (R) is   |
//| used, so a small price difference between feeds does not matter. |
//+------------------------------------------------------------------+
#property copyright "gold-agent"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

input string InpUrl          = "https://api.github.com/repos/basemayman-ux/gold-agent/contents/docs/data/signal.json";
input double InpRiskPercent  = 0.5;     // risk per trade, % of balance
input double InpMaxLots      = 0.10;    // hard cap on lot size
input double InpMaxSpread    = 1.0;     // skip if spread (price units) is wider
input double InpMaxFeedDiff  = 4.0;     // skip if broker price differs from the signal price by more ($)
input int    InpMaxPerDay    = 4;       // max trades per day
input int    InpMaxLossesDay = 2;       // stop for the day after this many losing trades
input int    InpPollSeconds  = 75;      // how often to read the signal (GitHub allows ~60 reads/hour)
input int    InpMaxAgeMin    = 8;       // ignore signals older than this
input double InpBreakevenR   = 0.7;     // move stop to entry at this multiple of R
input long   InpMagic        = 543480;
input bool   InpDemoOnly     = true;    // refuses to trade on a real account

CTrade trade;
datetime lastPoll = 0;
double   gR = 0;          // R (price distance) of the open trade
bool     gBE = false;

//--- tiny JSON helpers (no library needed) -----------------------------
string Token(const string &s, const string key, int from)
  {
   int p = StringFind(s, "\"" + key + "\"", from);
   if(p < 0) return "";
   p = StringFind(s, ":", p);
   if(p < 0) return "";
   p++;
   while(p < StringLen(s) && (StringGetCharacter(s, p) == ' ' || StringGetCharacter(s, p) == '\n' ||
         StringGetCharacter(s, p) == '\r' || StringGetCharacter(s, p) == '\t')) p++;
   int e = p;
   if(StringGetCharacter(s, p) == '"')
     {
      p++;
      e = StringFind(s, "\"", p);
      if(e < 0) return "";
      return StringSubstr(s, p, e - p);
     }
   while(e < StringLen(s))
     {
      ushort c = StringGetCharacter(s, e);
      if(c == ',' || c == '}' || c == '\n' || c == '\r') break;
      e++;
     }
   string t = StringSubstr(s, p, e - p);
   StringTrimRight(t);
   return t;
  }

// "2026-10-24T19:50:28.710268+00:00" -> UTC datetime
datetime IsoToUtc(string iso)
  {
   if(StringLen(iso) < 19) return 0;
   string t = StringSubstr(iso, 0, 4) + "." + StringSubstr(iso, 5, 2) + "." + StringSubstr(iso, 8, 2) + " " +
              StringSubstr(iso, 11, 8);
   return StringToTime(t);   // the feed is always +00:00
  }

bool Fetch(string &body)
  {
   char post[], result[];
   string headers = "Accept: application/vnd.github.raw\r\nUser-Agent: gold-agent-ea\r\n";
   string rh;
   ResetLastError();
   int code = WebRequest("GET", InpUrl, headers, 8000, post, result, rh);
   if(code != 200)
     {
      PrintFormat("Signal fetch failed: HTTP %d, error %d (is the URL allowed in Tools > Options > Expert Advisors?)",
                  code, GetLastError());
      return false;
     }
   body = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   return true;
  }

//--- helpers -------------------------------------------------------------
bool HavePosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk > 0 && PositionGetInteger(POSITION_MAGIC) == InpMagic && PositionGetString(POSITION_SYMBOL) == _Symbol)
         return true;
     }
   return false;
  }

void TodayStats(int &n, int &losses)
  {
   n = 0; losses = 0;
   MqlDateTime d; TimeToStruct(TimeCurrent(), d);
   d.hour = 0; d.min = 0; d.sec = 0;
   datetime from = StructToTime(d);
   if(!HistorySelect(from, TimeCurrent() + 60)) return;
   for(int i = 0; i < HistoryDealsTotal(); i++)
     {
      ulong tk = HistoryDealGetTicket(i);
      if(HistoryDealGetInteger(tk, DEAL_MAGIC) != InpMagic) continue;
      long entry = HistoryDealGetInteger(tk, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN) n++;
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
        {
         double pr = HistoryDealGetDouble(tk, DEAL_PROFIT) + HistoryDealGetDouble(tk, DEAL_COMMISSION) +
                     HistoryDealGetDouble(tk, DEAL_SWAP);
         double risk = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0;
         if(pr < -0.5 * risk) losses++;
        }
     }
  }

double LotsFor(double R, bool buy)
  {
   double risk = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0;
   double px = SymbolInfoDouble(_Symbol, buy ? SYMBOL_ASK : SYMBOL_BID);
   double loss = 0;
   if(!OrderCalcProfit(buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, 1.0, px, buy ? px - R : px + R, loss) || loss >= 0)
      return 0;
   double lots = risk / (-loss);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double mn = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double mx = MathMin(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX), InpMaxLots);
   lots = MathFloor(lots / step) * step;
   if(lots < mn) return 0;       // too small to hold the risk: skip rather than over-risk
   return MathMin(lots, mx);
  }

void Say(string m)
  {
   Print(m);
   SendNotification(m);          // optional: needs MetaQuotes ID in Tools > Options > Notifications
  }

//--- signal handling -------------------------------------------------------
void CheckSignal()
  {
   string b;
   if(!Fetch(b)) return;

   string dec = Token(b, "decision", 0);
   if(dec != "LONG" && dec != "SHORT") return;
   int pp = StringFind(b, "\"plan\"");
   if(pp < 0) return;
   string slS = Token(b, "sl", pp), enS = Token(b, "entry", pp), vuS = Token(b, "valid_until", pp);
   if(slS == "" || enS == "" || slS == "null") return;
   double sl = StringToDouble(slS), en = StringToDouble(enS);
   double R = MathAbs(en - sl);
   bool buy = (dec == "LONG");

   datetime gen = IsoToUtc(Token(b, "generated_utc", 0));
   datetime vu  = IsoToUtc(vuS);
   datetime now = TimeGMT();
   if(gen == 0 || now - gen > InpMaxAgeMin * 60) { return; }
   if(vu > 0 && now > vu) { return; }

   double key = sl * (buy ? 1 : -1);
   if(GlobalVariableCheck("GODO_lastkey") && MathAbs(GlobalVariableGet("GODO_lastkey") - key) < 0.005) return;

   string skip = "";
   int n, losses; TodayStats(n, losses);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID), ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(InpDemoOnly && AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO) skip = "not a demo account";
   else if(HavePosition()) skip = "a trade is already open";
   else if(n >= InpMaxPerDay) skip = "daily trade limit reached";
   else if(losses >= InpMaxLossesDay) skip = "daily loss stop reached";
   else if(ask - bid > InpMaxSpread) skip = StringFormat("spread too wide (%.2f)", ask - bid);
   else if(MathAbs((buy ? ask : bid) - en) > InpMaxFeedDiff) skip = StringFormat("broker price %.2f is far from signal %.2f", buy ? ask : bid, en);
   else if(R < 1.0) skip = "stop distance looks wrong";

   GlobalVariableSet("GODO_lastkey", key);     // never act twice on the same signal
   if(skip != "") { Say("MT5 DEMO: signal " + dec + " skipped: " + skip); return; }

   double lots = LotsFor(R, buy);
   if(lots <= 0) { Say("MT5 DEMO: signal " + dec + " skipped: lot size too small for the risk"); return; }

   double px  = buy ? ask : bid;
   double slp = NormalizeDouble(buy ? px - R : px + R, _Digits);
   double tpp = NormalizeDouble(buy ? px + R : px - R, _Digits);
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(50);
   trade.SetTypeFillingBySymbol(_Symbol);
   bool ok = buy ? trade.Buy(lots, _Symbol, 0, slp, tpp, "gold-agent")
                 : trade.Sell(lots, _Symbol, 0, slp, tpp, "gold-agent");
   if(ok && (trade.ResultRetcode() == TRADE_RETCODE_DONE || trade.ResultRetcode() == TRADE_RETCODE_PLACED))
     {
      gR = R; gBE = false;
      Say(StringFormat("MT5 DEMO: %s %.2f lots at %.2f, stop %.2f, target %.2f (R=$%.2f)", dec, lots, px, slp, tpp, R));
     }
   else
      Say(StringFormat("MT5 DEMO: order failed, retcode %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription()));
  }

void ManagePosition()
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong tk = PositionGetTicket(i);
      if(tk == 0 || PositionGetInteger(POSITION_MAGIC) != InpMagic || PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      bool buy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      double R = MathAbs(open - sl);
      if(gR > 0 && !gBE) R = gR;
      if(R <= 0) continue;
      bool alreadyBE = buy ? (sl >= open - 0.001) : (sl <= open + 0.001);
      if(alreadyBE) { gBE = true; continue; }
      double px = SymbolInfoDouble(_Symbol, buy ? SYMBOL_BID : SYMBOL_ASK);
      double moved = buy ? px - open : open - px;
      if(moved >= InpBreakevenR * R)
        {
         if(trade.PositionModify(tk, NormalizeDouble(open, _Digits), tp))
           { gBE = true; Say(StringFormat("MT5 DEMO: stop moved to entry %.2f (0.7R reached)", open)); }
        }
     }
  }

//--- events -----------------------------------------------------------------
int OnInit()
  {
   EventSetTimer(1);
   PrintFormat("GoldAgentEA started on %s, account %I64d. Remember: Tools > Options > Expert Advisors > Allow WebRequest for api.github.com",
               _Symbol, AccountInfoInteger(ACCOUNT_LOGIN));
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
  {
   ManagePosition();
   if(TimeLocal() - lastPoll >= InpPollSeconds)
     {
      lastPoll = TimeLocal();
      CheckSignal();
     }
  }
