import pandas as pd
import numpy as np

def compute_rsi(series, length=14):
    """Wilder-style RSI (same as TradingView's ta.rsi)."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_vwap(df):
    """Calculate daily resetting VWAP."""
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3
    dates = df.index.date
    vwap = (typical_price * df['Volume']).groupby(dates).cumsum() / df['Volume'].groupby(dates).cumsum()
    return vwap

def compute_bollinger_bands(series, length=20, num_std=3.0):
    """Returns (upper, middle, lower) Bollinger Bands."""
    middle = series.rolling(window=length).mean()
    std = series.rolling(window=length).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    return upper, middle, lower

def pandas_barssince(condition):
    """Calculates bars since condition was last True (vectorized)."""
    idx = pd.Series(range(len(condition)), index=condition.index)
    last_true_idx = idx.where(condition).ffill()
    return idx - last_true_idx

def pandas_valuewhen(condition, source, occurrence=1):
    """Returns source value at the n-th previous occurrence of condition."""
    source_true = source.where(condition).dropna()
    shifted = source_true.shift(occurrence).reindex(condition.index)
    return shifted.ffill()

def calculate_3_sigma_divergence(df, bb_length=20, bb_mult=3.0, rsi_length=14, lookback=15, daily_upper_bb=None, daily_lower_bb=None):
    """
    Executes Mind In Money Reversal Pro indicator logic on the input DataFrame.
    If daily_upper_bb and daily_lower_bb are passed, it checks 15m prices against these daily bands.
    """
    if len(df) < max(bb_length, rsi_length) + lookback + 5:
        df_res = df.copy()
        df_res['upper_bb'] = np.nan
        df_res['middle_bb'] = np.nan
        df_res['lower_bb'] = np.nan
        df_res['vwap'] = np.nan
        df_res['rsi'] = np.nan
        df_res['long_trigger'] = False
        df_res['short_trigger'] = False
        return df_res

    df_res = df.copy()
    
    if daily_upper_bb is not None and daily_lower_bb is not None:
        upper_bb = daily_upper_bb
        lower_bb = daily_lower_bb
        middle_bb = (daily_upper_bb + daily_lower_bb) / 2
        df_res['upper_bb'] = upper_bb
        df_res['lower_bb'] = lower_bb
        df_res['middle_bb'] = middle_bb
    else:
        upper_bb, middle_bb, lower_bb = compute_bollinger_bands(df_res['Close'], bb_length, bb_mult)
        df_res['upper_bb'] = upper_bb
        df_res['middle_bb'] = middle_bb
        df_res['lower_bb'] = lower_bb
        
    df_res['vwap'] = compute_vwap(df_res)
    df_res['rsi'] = compute_rsi(df_res['Close'], rsi_length)
    
    open_0 = df_res['Open']
    close_0 = df_res['Close']
    high_0 = df_res['High']
    low_0 = df_res['Low']
    
    open_1 = df_res['Open'].shift(1)
    close_1 = df_res['Close'].shift(1)
    high_1 = df_res['High'].shift(1)
    low_1 = df_res['Low'].shift(1)
    
    open_2 = df_res['Open'].shift(2)
    close_2 = df_res['Close'].shift(2)
    high_2 = df_res['High'].shift(2)
    low_2 = df_res['Low'].shift(2)
    
    close_3 = df_res['Close'].shift(3)
    
    is_bearish_2 = close_2 < open_2
    is_small_body_1 = (close_1 - open_1).abs() < (high_1 - low_1) * 0.3
    is_bullish_0 = (close_0 > open_0) & (close_0 > (open_2 + close_2) / 2)
    morning_star = is_bearish_2 & is_small_body_1 & is_bullish_0
    
    is_bearish_1 = close_1 < open_1
    is_bearish_0 = close_0 < open_0
    three_black_crows = (
        is_bearish_2 & 
        is_bearish_1 & 
        is_bearish_0 & 
        (close_2 < close_3) & 
        (close_1 < close_2) & 
        (close_0 < close_1)
    )
    
    lower_pierce = low_0 < lower_bb
    recent_lower_pierce = pandas_barssince(lower_pierce.shift(1)) <= lookback
    rsi_at_prev_low = pandas_valuewhen(lower_pierce, df_res['rsi'], 1)
    bullish_divergence = df_res['rsi'] > rsi_at_prev_low
    df_res['long_trigger'] = recent_lower_pierce & bullish_divergence & morning_star & (low_0 <= lower_bb * 1.01)
    
    upper_pierce = high_0 > upper_bb
    recent_upper_pierce = pandas_barssince(upper_pierce.shift(1)) <= lookback
    rsi_at_prev_high = pandas_valuewhen(upper_pierce, df_res['rsi'], 1)
    bearish_divergence = df_res['rsi'] < rsi_at_prev_high
    df_res['short_trigger'] = recent_upper_pierce & bearish_divergence & three_black_crows & (high_0 >= upper_bb * 0.99)
    
    df_res['long_trigger'] = df_res['long_trigger'].fillna(False).astype(bool)
    df_res['short_trigger'] = df_res['short_trigger'].fillna(False).astype(bool)
    
    return df_res
