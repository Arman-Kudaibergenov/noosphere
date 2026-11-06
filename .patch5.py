import io, sys
p="client/web/index.html"
s=io.open(p,encoding="utf-8").read()
old=".sendbtn .sic svg{width:10px;height:10px;stroke-width:2.6}\n"
new=(".sendbtn .sic svg{width:10px;height:10px;stroke-width:2.6}\n"
     "/* sparkles агента мельче остальных глифов не читаются — чуть крупнее */\n"
     ".sendbtn .sic[data-k=\"agent\"] svg{width:11px;height:11px;stroke-width:2.2}\n")
assert s.count(old)==1
io.open(p,"w",encoding="utf-8").write(s.replace(old,new))
print("ok")
