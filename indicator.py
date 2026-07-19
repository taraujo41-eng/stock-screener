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

def detect_bb_pct_b_divergence(price_series, bb_pct_b_series, lookback=20):
    """
    Check for Bollinger Bands %B Divergence in the last `lookback` periods.
    Returns (bull_div, bear_div)
    """
    if len(price_series) < lookback + 2:
        return False, False
        
    curr_price = float(price_series.iloc[-1])
    curr_pct_b = float(bb_pct_b_series.iloc[-1])
    
    # The lookback window (excluding last 2 candles for distinct swing)
    window_price = price_series.iloc[-(lookback+2):-2]
    window_pct_b = bb_pct_b_series.iloc[-(lookback+2):-2]
    
    # Find the index position of the swing low/high
    lowest_idx = window_price.values.argmin()
    highest_idx = window_price.values.argmax()
    
    bars_from_low = len(window_price) - lowest_idx
    bars_from_high = len(window_price) - highest_idx
    
    lowest_price_in_window = float(window_price.iloc[lowest_idx])
    highest_price_in_window = float(window_price.iloc[highest_idx])
    
    pct_b_at_low = float(window_pct_b.iloc[lowest_idx])
    pct_b_at_high = float(window_pct_b.iloc[highest_idx])

    # Bullish Divergence: Price lower low + %B higher low
    pct_b_bull_magnitude = curr_pct_b - pct_b_at_low
    bull_div = (
        (curr_price < lowest_price_in_window) and
        (curr_pct_b > pct_b_at_low) and
        (pct_b_bull_magnitude >= 5) and  # at least 5% divergence
        (curr_pct_b <= 35) and            # must be in lower region
        (bars_from_low >= 5)
    )
    
    # Bearish Divergence: Price higher high + %B lower high
    pct_b_bear_magnitude = pct_b_at_high - curr_pct_b
    bear_div = (
        (curr_price > highest_price_in_window) and
        (curr_pct_b < pct_b_at_high) and
        (pct_b_bear_magnitude >= 5) and  # at least 5% divergence
        (curr_pct_b >= 65) and            # must be in upper region
        (bars_from_high >= 5)
    )
    
    return bull_div, bear_div

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
    
    # Compute %B Bollinger Bands
    band_width = upper_bb - lower_bb
    if isinstance(band_width, pd.Series):
        bb_pct_b = ((df_res['Close'] - lower_bb) / band_width.replace(0, np.nan)) * 100
        bb_pct_b = bb_pct_b.fillna(50.0)
    else:
        if band_width != 0:
            bb_pct_b = ((df_res['Close'] - lower_bb) / band_width) * 100
        else:
            bb_pct_b = 50.0

    # Calculate rolling %B on the active series itself to check for %B divergence
    middle_rolling = df_res['Close'].rolling(window=bb_length).mean()
    std_rolling = df_res['Close'].rolling(window=bb_length).std()
    upper_rolling = middle_rolling + bb_mult * std_rolling
    lower_rolling = middle_rolling - bb_mult * std_rolling
    band_width_rolling = upper_rolling - lower_rolling
    bb_pct_b_rolling = ((df_res['Close'] - lower_rolling) / band_width_rolling.replace(0, np.nan)) * 100
    bb_pct_b_rolling = bb_pct_b_rolling.fillna(50.0)
    bb_div_bull, bb_div_bear = detect_bb_pct_b_divergence(df_res['Close'], bb_pct_b_rolling, lookback=lookback)

    df_res['long_trigger'] = (close_0 <= lower_bb) | (bb_pct_b <= 10)
    df_res['short_trigger'] = (close_0 >= upper_bb) | (bb_pct_b >= 90)
    
    df_res['long_trigger'] = df_res['long_trigger'].fillna(False).astype(bool)
    df_res['short_trigger'] = df_res['short_trigger'].fillna(False).astype(bool)

    if bb_div_bull:
        df_res.loc[df_res.index[-1], 'long_trigger'] = True
    if bb_div_bear:
        df_res.loc[df_res.index[-1], 'short_trigger'] = True
    
    return df_res
