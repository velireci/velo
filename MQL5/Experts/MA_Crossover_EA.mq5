//+------------------------------------------------------------------+
//|                                            MA_Crossover_EA.mq5   |
//|                                                                  |
//|  A simple Expert Advisor for MetaTrader 5.                       |
//|                                                                  |
//|  The idea in one sentence:                                       |
//|    When a fast moving average crosses ABOVE a slow moving        |
//|    average we buy, and when it crosses BELOW we sell.            |
//|                                                                  |
//|  House rules:                                                    |
//|    - Only one position may be open at any time, and any open     |
//|      position on the symbol counts, whoever opened it.           |
//|    - Every trade gets a fixed stop loss and take profit.         |
//|    - Signals are only read from bars that have already CLOSED,   |
//|      so a signal never changes its mind halfway through a bar.   |
//+------------------------------------------------------------------+
#property copyright "velireci"
#property version   "1.00"
#property description "Buys when the fast MA crosses above the slow MA, sells when it crosses below."
#property strict

// CTrade is a ready-made helper class from MetaTrader that knows how to
// send buy/sell orders, so we do not have to fill in order requests by hand.
#include <Trade\Trade.mqh>

//+------------------------------------------------------------------+
//| Input parameters                                                 |
//| These show up in the EA's settings window, so they can be changed|
//| without touching the code.                                       |
//+------------------------------------------------------------------+
input double        InpLots        = 0.01;            // Lot size (trade volume)
input int           InpFastPeriod  = 20;              // Fast MA period
input int           InpSlowPeriod  = 50;              // Slow MA period
input ENUM_MA_METHOD InpMaMethod   = MODE_SMA;        // MA method (simple, exponential, ...)
input ENUM_APPLIED_PRICE InpMaPrice = PRICE_CLOSE;    // Price the MAs are built from
input int           InpStopLoss    = 300;             // Stop loss in points (0 = none)
input int           InpTakeProfit  = 600;             // Take profit in points (0 = none)
input ulong         InpSlippage    = 10;              // Max allowed price slippage in points
input long          InpMagic       = 20260101;        // Magic number (tags our own trades)

//+------------------------------------------------------------------+
//| Global variables                                                 |
//+------------------------------------------------------------------+
CTrade   trade;                 // our order-sending helper
int      handleFastMa = INVALID_HANDLE;  // reference to the fast MA indicator
int      handleSlowMa = INVALID_HANDLE;  // reference to the slow MA indicator
datetime lastBarTime  = 0;      // open time of the last bar we already looked at

//+------------------------------------------------------------------+
//| OnInit: runs once when the EA is attached to a chart.            |
//| We prepare the indicators and the trade helper here.             |
//+------------------------------------------------------------------+
int OnInit()
{
   // The fast MA must really be faster (shorter) than the slow one,
   // otherwise the "crossover" idea makes no sense.
   if(InpFastPeriod < 1 || InpSlowPeriod < 1)
   {
      Print("Init failed: MA periods must be 1 or greater.");
      return(INIT_PARAMETERS_INCORRECT);
   }

   if(InpFastPeriod >= InpSlowPeriod)
   {
      Print("Init failed: the fast MA period must be smaller than the slow MA period.");
      return(INIT_PARAMETERS_INCORRECT);
   }

   if(InpLots <= 0.0)
   {
      Print("Init failed: lot size must be greater than zero.");
      return(INIT_PARAMETERS_INCORRECT);
   }

   // Create the two moving averages on the chart's symbol and timeframe.
   handleFastMa = iMA(_Symbol, _Period, InpFastPeriod, 0, InpMaMethod, InpMaPrice);
   handleSlowMa = iMA(_Symbol, _Period, InpSlowPeriod, 0, InpMaMethod, InpMaPrice);

   if(handleFastMa == INVALID_HANDLE || handleSlowMa == INVALID_HANDLE)
   {
      Print("Init failed: could not create the moving average indicators.");
      return(INIT_FAILED);
   }

   // Tell the trade helper how to send our orders.
   trade.SetExpertMagicNumber(InpMagic);       // so we can recognise our own trades later
   trade.SetDeviationInPoints(InpSlippage);    // how much price movement we tolerate
   trade.SetTypeFillingBySymbol(_Symbol);      // use a filling mode the symbol accepts
   trade.LogLevel(LOG_LEVEL_ERRORS);           // keep the log quiet unless something fails

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| OnDeinit: runs once when the EA is removed.                      |
//| We give the indicator handles back to the terminal.              |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   if(handleFastMa != INVALID_HANDLE)
      IndicatorRelease(handleFastMa);

   if(handleSlowMa != INVALID_HANDLE)
      IndicatorRelease(handleSlowMa);
}

//+------------------------------------------------------------------+
//| OnTick: runs on every incoming price change.                     |
//| All of the actual decision making starts here.                   |
//+------------------------------------------------------------------+
void OnTick()
{
   // We only want to act once per bar, right after a new bar opens.
   // That way the crossover we read is based on finished bars only.
   if(!IsNewBar())
      return;

   // House rule: never trade while anything is already open on this
   // symbol, no matter whether this EA, another EA or you opened it.
   if(HasOpenPosition())
      return;

   double fastPrev = 0.0, fastOlder = 0.0;   // fast MA on bar 1 and bar 2
   double slowPrev = 0.0, slowOlder = 0.0;   // slow MA on bar 1 and bar 2

   if(!ReadMaValues(fastPrev, fastOlder, slowPrev, slowOlder))
      return;   // indicator data not ready yet, we simply wait for the next bar

   // A cross UP means: one bar ago the fast MA was still below (or equal to)
   // the slow MA, and on the latest finished bar it is above it.
   bool crossUp   = (fastOlder <= slowOlder && fastPrev > slowPrev);

   // A cross DOWN is the mirror image of that.
   bool crossDown = (fastOlder >= slowOlder && fastPrev < slowPrev);

   if(crossUp)
      OpenTrade(ORDER_TYPE_BUY);
   else if(crossDown)
      OpenTrade(ORDER_TYPE_SELL);
}

//+------------------------------------------------------------------+
//| IsNewBar: returns true only on the first tick of a new bar.      |
//+------------------------------------------------------------------+
bool IsNewBar()
{
   datetime currentBarTime = (datetime)SeriesInfoInteger(_Symbol, _Period, SERIES_LASTBAR_DATE);

   if(currentBarTime == 0 || currentBarTime == lastBarTime)
      return(false);

   lastBarTime = currentBarTime;
   return(true);
}

//+------------------------------------------------------------------+
//| HasOpenPosition: true if ANY position is open on this symbol.    |
//| It does not matter who opened it: this EA, another EA, or you    |
//| by hand. While anything is open on this symbol, the EA waits.    |
//+------------------------------------------------------------------+
bool HasOpenPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;   // the position list changed while we were reading it

      if(PositionGetString(POSITION_SYMBOL) == _Symbol)
         return(true);
   }

   return(false);
}

//+------------------------------------------------------------------+
//| ReadMaValues: copies the MA values of the two latest CLOSED bars.|
//|   index 1 = the bar that just finished ("prev")                  |
//|   index 2 = the bar before that        ("older")                 |
//| Returns false when the indicator has no data yet.                |
//+------------------------------------------------------------------+
bool ReadMaValues(double &fastPrev, double &fastOlder,
                  double &slowPrev, double &slowOlder)
{
   double fastBuffer[], slowBuffer[];

   // Start at bar 1 and take 2 values, so bar 0 (the unfinished bar) is skipped.
   if(CopyBuffer(handleFastMa, 0, 1, 2, fastBuffer) < 2)
      return(false);

   if(CopyBuffer(handleSlowMa, 0, 1, 2, slowBuffer) < 2)
      return(false);

   // CopyBuffer fills the array oldest first, so [0] is bar 1 and [1] is bar 2.
   fastPrev  = fastBuffer[0];
   fastOlder = fastBuffer[1];
   slowPrev  = slowBuffer[0];
   slowOlder = slowBuffer[1];

   return(true);
}

//+------------------------------------------------------------------+
//| OpenTrade: sends one market order with stop loss and take profit.|
//+------------------------------------------------------------------+
void OpenTrade(const ENUM_ORDER_TYPE orderType)
{
   // Current market prices: we buy at Ask and sell at Bid.
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   if(ask <= 0.0 || bid <= 0.0)
      return;   // no valid quote available right now

   // How big one "point" is for this symbol, and how many decimals it uses.
   double point  = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int    digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);

   double entry = (orderType == ORDER_TYPE_BUY) ? ask : bid;
   double sl    = 0.0;
   double tp    = 0.0;

   if(orderType == ORDER_TYPE_BUY)
   {
      // For a buy, the stop is below the entry and the target is above it.
      if(InpStopLoss > 0)
         sl = NormalizeDouble(entry - InpStopLoss * point, digits);
      if(InpTakeProfit > 0)
         tp = NormalizeDouble(entry + InpTakeProfit * point, digits);
   }
   else
   {
      // For a sell, it is the other way round.
      if(InpStopLoss > 0)
         sl = NormalizeDouble(entry + InpStopLoss * point, digits);
      if(InpTakeProfit > 0)
         tp = NormalizeDouble(entry - InpTakeProfit * point, digits);
   }

   // Round the volume so it matches what the broker accepts.
   double volume = NormalizeVolume(InpLots);
   if(volume <= 0.0)
   {
      Print("Trade skipped: the requested lot size is not valid for this symbol.");
      return;
   }

   bool sent = (orderType == ORDER_TYPE_BUY)
               ? trade.Buy(volume, _Symbol, 0.0, sl, tp, "MA crossover buy")
               : trade.Sell(volume, _Symbol, 0.0, sl, tp, "MA crossover sell");

   if(!sent)
      PrintFormat("Order failed. Retcode=%d (%s)",
                  trade.ResultRetcode(), trade.ResultRetcodeDescription());
}

//+------------------------------------------------------------------+
//| NormalizeVolume: fits the wanted lot size into the broker's      |
//| minimum, maximum and step. For example 0.013 lots becomes 0.01   |
//| when the step is 0.01.                                           |
//+------------------------------------------------------------------+
double NormalizeVolume(double volume)
{
   double minVolume  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxVolume  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double stepVolume = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);

   if(stepVolume > 0.0)
      volume = MathFloor(volume / stepVolume) * stepVolume;

   if(volume < minVolume)
      volume = minVolume;

   if(maxVolume > 0.0 && volume > maxVolume)
      volume = maxVolume;

   // Keep the same number of decimals as the volume step (usually 2).
   int volumeDigits = (stepVolume > 0.0) ? (int)MathMax(0, -MathLog10(stepVolume)) : 2;
   return(NormalizeDouble(volume, volumeDigits));
}
//+------------------------------------------------------------------+
