"""Anchor the model's hourly temperatures to the official daily outlook.

The header prints the outlook's maximum ("HIGH TODAY 21°"), but the hourly
strip, the coming hour and its facts read the model's hourly curve, whose
peak can be a few degrees off (18°). `anchor_temps` rescales each day's
hourly temperatures so the curve agrees with the outlook while keeping the
model's *shape*: when the day warms, when it peaks, when it cools.

Pure: domain data in, the same Forecast out (its days' hours replaced).
Nothing here knows which provider filled either in.

For each day the outlook covers:

- m_hi is the model's peak over MAX_WINDOW (6 am to 9 pm, the hours the
  chart draws), m_lo its trough over MIN_WINDOW (midnight to 9 am: the
  overnight minimum an outlook's minimum describes). The windows overlap,
  so m_lo <= m_hi always.
- With both an outlook max and min, temperatures between m_lo and m_hi map
  linearly onto [min, max]: the peak becomes the outlook's max exactly and
  the overnight trough its min. Beyond those two points (a late evening
  cooler than the dawn, say) the map continues with slope 1, so a narrow
  model day stretched to a wide outlook never throws an outlying hour
  degrees past the outlook.
- With only one of them (an outlook often drops today's minimum after the
  morning), every hour shifts by the same amount so that end matches.
- A flat model day (m_hi == m_lo) cannot be stretched; it shifts to the max
  (or to the min, without a max).
- An outlook max at or below its own min (a cold change through the day)
  cannot be reached by an order-preserving map that also hits the min, so
  the max wins: it is the figure the poster prints for the day.

Every case is a continuous, increasing map of temperature, so the hours
keep their order and the peak and trough stay at the model's hours.
Applying it twice changes nothing.
"""

from dataclasses import replace

MAX_WINDOW = range(6, 22)  # 6 am .. 9 pm inclusive: the chart's hours
MIN_WINDOW = range(0, 10)  # midnight .. 9 am inclusive: the overnight low


def temp_map(m_lo, m_hi, t_min=None, t_max=None):
    """The function t -> anchored t for one day (see the module rules), or
    None when there is nothing to anchor to."""
    use_max = t_max is not None and m_hi is not None
    use_min = t_min is not None and m_lo is not None
    if use_max and use_min and m_hi > m_lo and t_max > t_min:
        scale = (t_max - t_min) / (m_hi - m_lo)

        def f(t):
            if t == m_hi:
                return t_max
            if t == m_lo:
                return t_min
            if t > m_hi:
                return t_max + (t - m_hi)
            if t < m_lo:
                return t_min + (t - m_lo)
            return t_min + (t - m_lo) * scale

        return f
    if use_max:
        shift = t_max - m_hi
        return lambda t: t_max if t == m_hi else t + shift
    if use_min:
        shift = t_min - m_lo
        return lambda t: t_min if t == m_lo else t + shift
    return None


def anchor_day(day_fc, outlook):
    """Rescale one Day's hourly temperatures to a DayOutlook (in place)."""
    if day_fc is None or outlook is None or not day_fc.hours:
        return day_fc
    peak = [h.temp for h in day_fc.hours if h.hour in MAX_WINDOW]
    night = [h.temp for h in day_fc.hours if h.hour in MIN_WINDOW]
    f = temp_map(min(night) if night else None, max(peak) if peak else None, outlook.t_min, outlook.t_max)
    if f is not None:
        day_fc.hours = [replace(h, temp=f(h.temp)) for h in day_fc.hours]
    return day_fc


def anchor_temps(forecast, outlooks):
    """Anchor every day of `forecast` that `outlooks` ({date: DayOutlook})
    covers. Every outlook day, not only the poster's: the evening poster
    describes tomorrow while its coming hour is still today's."""
    if forecast is None or not outlooks:
        return forecast
    for d, outlook in outlooks.items():
        anchor_day(forecast.day(d), outlook)
    return forecast
