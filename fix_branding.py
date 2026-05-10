f = open('RaanuTradingBot.html', 'r', encoding='utf-8')
c = f.read()
f.close()

c = c.replace('AlgoTrader v2.1 \u2014 Trade 212', 'RaanuTradingBot \u2014 Trade 212')
c = c.replace('Algo<em>Trader</em>', 'Raanu<em>TradingBot</em>')
c = c.replace('AlgoTrader v2.1 starting', 'RaanuTradingBot v2.1 starting')

f = open('RaanuTradingBot.html', 'w', encoding='utf-8')
f.write(c)
f.close()

print('Done! Branding fixed.')
