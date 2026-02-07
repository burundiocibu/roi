# roi notes

# Feature list

## lmidb.py

Manages the database of transactions, retrieved positions, and candle data. 

## roi.py

Used to generate reports on various intervals of time: monthly, quarterly, yearly, and over all time

Computes various quantites for all accounts

  * positions over time
  * cost basis over time 
  * income over periods

### Todo

  * handle cost basis for securities w/o quotes (CUISP only)
  * make roi actualy display roi
  * make income also report a percent versus investment
  * make a value over time report (w an an account total)
  * make a unrealized gains report
  * make a taxable gains report

### testing of roi.py

  * make sure all positions stay positive
    done for all accouints
  * older retrieved positions match computed positions
    will have to wait a little for this
  * positions match old statements
    done for deb-inv for Jan 31, 2022 statement, all positions except cash match
  * cost basis is _close_ to shwab reported in the security detail flyover that shows lots
    done for deb_inv for most securities; need to handle ones with out candles

## Schwab api notes
So the schwab api needs an oath token and the schwab_py package automates getting one
When prompted to login at schwab, use the trading account not the developer account

Trader API: https://developer.schwab.com/products/trader-api--individual/details/specifications/Retail%20Trader%20API%20Production
Market data: https://developer.schwab.com/products/trader-api--individual/details/specifications/Market%20Data%20Production

Created this bearer token 9/19/2025
curl -X 'GET' \                                                                       130 main!?
  'https://api.schwabapi.com/trader/v1/accounts/FE02FDF069EC9EB756E9DAB937AC0C21F015323B83E155D07046F3A36512D31C/transactions?startDate=2025-06-01T00%3A00%3A00.000Z&endDate=2025-08-01T00%3A00%3A00.000Z&types=TRADE' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer I0.b2F1dGgyLmNkYy5zY2h3YWIuY29t.4R_hr9v4MotsVpSsoEqAkV7jRZhvkNTr2lg6IUQQUX8@'

Note that the account hash is tied to the bearer token...

## CUISP investments 

78017FA45
48133DMA5
78017FAF0
61776WBL6
40055QCU4
40054LRA4