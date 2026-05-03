# Operators Snapshot v1 (R7-0)

Generated: 2026-05-03T06:15:00.926356Z
Total active operators: 66

## By category

### Arithmetic (15)

| Name | Definition | Description |
|---|---|---|
| `abs` | `abs(x)` | Returns the absolute value of a number, removing any negative sign. |
| `add` | `add(x, y, filter = false), x + y` | Adds two or more inputs element wise. Set filter=true to treat NaNs as 0 before summing. |
| `densify` | `densify(x)` | Converts a grouping field of many buckets into lesser number of only available buckets so as to make working with groupi... |
| `divide` | `divide(x, y), x / y` | x / y |
| `inverse` | `inverse(x)` | 1 / x |
| `log` | `log(x)` | Calculates the natural logarithm of the input value. Commonly used to transform data that has positive values. |
| `max` | `max(x, y, ..)` | Maximum value of all inputs. At least 2 inputs are required |
| `min` | `min(x, y ..)` | Minimum value of all inputs. At least 2 inputs are required |
| `multiply` | `multiply(x ,y, ... , filter=false), x * y` | Multiplies two or more inputs element wise. Set filter=true to treat NaNs as 0 before multiplication |
| `power` | `power(x, y)` | x ^ y |
| `reverse` | `reverse(x)` |  - x |
| `sign` | `sign(x)` | Returns the sign of a number: +1 for positive, -1 for negative, and 0 for zero. If the input is NaN, returns NaN.  Inp... |
| `signed_power` | `signed_power(x, y)` | x raised to the power of y such that final result preserves sign of x |
| `sqrt` | `sqrt(x)` | Returns the non negative square root of x. Equivalent to power(x, 0.5); for signed roots use signed_power(x, 0.5). |
| `subtract` | `subtract(x, y, filter=false), x - y` | Subtracts inputs left to right: x ? y ? … Supports two or more inputs. Set filter=true to treat NaNs as 0 before subtrac... |

### Cross Sectional (6)

| Name | Definition | Description |
|---|---|---|
| `normalize` | `normalize(x, useStd = false, limit = 0.0)` | Centers a daily cross section by subtracting the market mean; optionally divide by the cross sectional standard deviatio... |
| `quantile` | `quantile(x, driver = gaussian, sigma = 1.0)` | Ranks and shifts a vector of Alpha values, then applies a chosen statistical distribution (gaussian, cauchy, or uniform)... |
| `rank` | `rank(x, rate=2)` | Ranks the values of the input x among all instruments, returning numbers evenly spaced between 0.0 and 1.0. Useful for n... |
| `scale` | `scale(x, scale=1, longscale=1, shortscale=1)` | Scales the input so that the sum of absolute values across all instruments equals a specified book size. Allows separate... |
| `winsorize` | `winsorize(x, std=4)` | Winsorize limits values in a data to within a specified number of standard deviations from the mean, reducing the impact... |
| `zscore` | `zscore(x)` | Z-score is a numerical measurement that describes a value's relationship to the mean of a group of values. Z-score is me... |

### Group (6)

| Name | Definition | Description |
|---|---|---|
| `group_backfill` | `group_backfill(x, group, d, std = 4.0)` | Fills missing (NaN) values for instruments within the same group by calculating a winsorized mean of all non-NaN values ... |
| `group_mean` | `group_mean(x, weight, group)` | Calculates the harmonic mean of a data field within each specified group. |
| `group_neutralize` | `group_neutralize(x, group)` | Neutralizes Alpha values within each specified group by subtracting the group mean from each value. Groups can be indust... |
| `group_rank` | `group_rank(x, group)` | Ranks each element within its group based on the input field, assigning a value between 0.0 and 1.0. This helps compare ... |
| `group_scale` | `group_scale(x, group)` | Normalizes values within each group to a range between 0 and 1, making data comparable across different groups. |
| `group_zscore` | `group_zscore(x, group)` | Calculates the Z-score of each value within its group, showing how far each value is from the group mean in terms of sta... |

### Logical (11)

| Name | Definition | Description |
|---|---|---|
| `and` | `and(input1, input2)` | Returns 1 ('true') if both inputs are 1 ('true'). Otherwise, returns 0 ('false'). |
| `equal` | `input1 == input2` | Returns 1 ('true') if input1 and input2 are the same. Otherwise, returns 0 ('false'). |
| `greater` | `input1 > input2` | Returns 1 ('true') if input1 is a larger than input2. Otherwise, returns 0 ('false'). |
| `greater_equal` | `input1 >= input2` | Returns 1 ('true') if input1 is a larger or the same as input2. Otherwise, returns 0 ('false'). |
| `if_else` | `if_else(input1, input2, input 3)` | The if_else operator returns one of two values based on a condition. If the condition is true, it returns the first valu... |
| `is_nan` | `is_nan(input)` | If (input == NaN) return 1 else return 0 |
| `less` | `input1 < input2` | Returns 1 ('true') if input1 is a smaller than input2. Otherwise, returns 0 ('false'). |
| `less_equal` | `input1 <= input2` | Returns 1 ('true') if input1 is a smaller or the same as input2. Otherwise, returns 0 ('false'). |
| `not` | `not(x)` | Returns the logical negation of x. Returns 0 when x is 1 (‘true’) and 1 when x is 0 (‘false’). |
| `not_equal` | `input1!= input2` | Returns 1 ('true') if input1 and input2 are different numbers. Otherwise, returns 0 ('false'). |
| `or` | `or(input1, input2)` | Returns 1 if either input is true (either input1 or input2 has a value of 1), otherwise it returns 0. |

### Time Series (24)

| Name | Definition | Description |
|---|---|---|
| `days_from_last_change` | `days_from_last_change(x)` | Calculates the number of days since the last change in the value of a given variable. |
| `hump` | `hump(x, hump = 0.01)` | Limits amount and magnitude of changes in input (thus reducing turnover) |
| `kth_element` | `kth_element(x, d, k, ignore=“NaN”)` | Returns the K-th value from a time series by looking back over a specified number of (‘d’) days, with the option to igno... |
| `last_diff_value` | `last_diff_value(x, d)` | Returns the most recent value of x from the past d days that is different from the current value of x. |
| `ts_arg_max` | `ts_arg_max(x, d)` | Returns the number of days since the maximum value occurred in the last d days of a time series. If today's value is the... |
| `ts_arg_min` | `ts_arg_min(x, d)` | Returns the number of days since the minimum value occurred in a time series over the past d days. If today's value is t... |
| `ts_av_diff` | `ts_av_diff(x, d)` | Calculates the difference between a value and its mean over a specified period, ignoring NaN values in the mean calculat... |
| `ts_backfill` | `ts_backfill(x,lookback = d, k=1)` | Replaces missing (NaN) values in a time series with the most recent valid value from a specified lookback window, improv... |
| `ts_corr` | `ts_corr(x, y, d)` | Calculates the Pearson correlation between two variables, x and y, over the past d days, showing how closely they move t... |
| `ts_count_nans` | `ts_count_nans(x ,d)` | Counts the number of missing (NaN) values in a data series over a specified number of days. |
| `ts_covariance` | `ts_covariance(y, x, d)` | Calculates the covariance between two time-series variables, y and x, over the past d days. Useful for measuring how two... |
| `ts_decay_linear` | `ts_decay_linear(x, d, dense = false)` | Applies a linear decay to time-series data over a set number of days, smoothing the data by averaging recent values and ... |
| `ts_delay` | `ts_delay(x, d)` | Returns the value of a variable x from d days ago. Use this operator to access historical data points by specifying the ... |
| `ts_delta` | `ts_delta(x, d)` | Calculates the difference between a value and its delayed version over a specified period. Useful for measuring changes ... |
| `ts_mean` | `ts_mean(x, d)` | Calculates the simple average (mean) value of a variable x over the past d days. |
| `ts_product` | `ts_product(x, d)` | Returns the product of the values of x over the past d days. Useful for calculating geometric means and compounding retu... |
| `ts_quantile` | `ts_quantile(x,d, driver="gaussian" )` | Calculates the ts_rank of the input and transforms it using the inverse cumulative distribution function (quantile funct... |
| `ts_rank` | `ts_rank(x, d, constant = 0)` | Ranks the value of a variable for each instrument over a specified number of past days, returning the rank of the curren... |
| `ts_regression` | `ts_regression(y, x, d, lag = 0, rettype = 0)` | Returns various parameters related to regression function |
| `ts_scale` | `ts_scale(x, d, constant = 0)` | Scales a time series to a 0–1 range based on its minimum and maximum values over a specified period, with an optional co... |
| `ts_std_dev` | `ts_std_dev(x, d)` | Calculates the standard deviation of a data series x over the past d days, measuring how much the values deviate from th... |
| `ts_step` | `ts_step(1)` | Returns a counter of days, incrementing by one each day. |
| `ts_sum` | `ts_sum(x, d)` | Sum values of x for the past d days. |
| `ts_zscore` | `ts_zscore(x, d)` | Calculates the Z-score of a time series, showing how far today's value is from the recent average, measured in standard ... |

### Transformational (2)

| Name | Definition | Description |
|---|---|---|
| `bucket` | `bucket(rank(x), range=“0, 1, 0.1”, skipBoth=False, NaNGroup=False) or bucket(rank(x), buckets = “2,5,6,7,10”, skipBoth=False, NaNGroup=False)` | The bucket operator creates custom groups by dividing data into buckets (ranges) based on ranked values of any data fiel... |
| `trade_when` | `trade_when(x, y, z)` | The trade_when operator changes Alpha values only when a specific condition is met, keeps previous values otherwise, and... |

### Vector (2)

| Name | Definition | Description |
|---|---|---|
| `vec_avg` | `vec_avg(x)` | Calculates the mean (average) of all elements in a vector field for each instrument and date, converting vector data to ... |
| `vec_sum` | `vec_sum(x)` | Calculates the sum of all values in a vector field. |

## Plan v5+ §R7-0 cross-check

- Plan-mentioned operators: 47
- Present in DB: 43
- Missing in DB: 4
- Available in DB beyond plan list: 23

### ❌ Missing operators (plan references these but not in DB)

- `group_demean`
- `group_normalize`
- `ts_max`
- `ts_min`

### Available operators not in plan list

- `and`
- `bucket`
- `days_from_last_change`
- `densify`
- `greater_equal`
- `group_backfill`
- `hump`
- `inverse`
- `is_nan`
- `kth_element`
- `last_diff_value`
- `less_equal`
- `log`
- `max`
- `not`
- `not_equal`
- `or`
- `power`
- `reverse`
- `sign`
- `sqrt`
- `vec_avg`
- `vec_sum`
