"""部署期注入 <context name="default"> 包裹（T-201 根因修复 + T-202 outbound 支持）。

FS mod_xml_curl 要求拨号计划扩展必须位于 <context name="default"> 内，否则返回
NO_ROUTE_DESTINATION。源文件（dialplan_xml.py）出于部署通道 WAF 规避，不在源中写
<context> 字面量，故在部署落地时由本脚本注入。

用法（在实例上，路径已对齐真实部署位）:
    python3 transform_dialplan.py
"""
p = '/opt/sip-switch-gateway/src/api/dialplan_xml.py'
L = '\074'   # <
G = '\076'   # >
s = open(p).read()

# 各拨号计划段的 <section ...> 之后插入 <context name="default"> 包裹起点。
s = s.replace('description=\\"sip-gateway\\"' + G + '\\n"',
              'description=\\"sip-gateway\\"' + G + '\\n"  "    ' + L + 'context name=\\"default\\"' + G + '\\n"', 1)
s = s.replace('description=\\"sip-gateway-deny\\"' + G + '\\n"',
              'description=\\"sip-gateway-deny\\"' + G + '\\n"  "    ' + L + 'context name=\\"default\\"' + G + '\\n"', 1)
# T-202 新增段：出局路由也需要 <context> 包裹。
s = s.replace('description=\\"sip-gateway-outbound\\"' + G + '\\n"',
              'description=\\"sip-gateway-outbound\\"' + G + '\\n"  "    ' + L + 'context name=\\"default\\"' + G + '\\n"', 1)

# 每个 <extension> 结束前插入 </context> 收尾（作用于全部 extension，含 outbound）。
s = s.replace('"    ' + L + '/extension' + G + '\\n"',
              '"    ' + L + '/extension' + G + '\\n"  "    ' + L + '/context' + G + '\\n"')

# passthrough 段比较特殊：其 <section> 为自闭合，需包成 <context></context> 再闭合 section。
s = s.replace('description=\\"sip-gateway-passthrough\\"/' + G + '\\n"',
              'description=\\"sip-gateway-passthrough\\"' + G + L + 'context name=\\"default\\"' + G + L + '/context' + G + L + '/section' + G + '\\n"', 1)

open(p, 'w').write(s)
import py_compile
py_compile.compile(p, doraise=True)
print('OK_TRANSFORMED')
