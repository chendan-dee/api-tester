#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据生成器 - 批量生成各类仿真测试数据
用法:
  python data_generator.py table --rows 100
  python data_generator.py gen --type 手机号 --count 50
  python data_generator.py api --name 用户列表 --count 20
  python data_generator.py template --config template.json --rows 200
"""

import argparse
import json
import os
import random
import re
import string
import sys
from datetime import datetime, timedelta

# Windows 终端默认 GBK 编码，emoji 会崩溃，强制 UTF-8
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ============================================================
# 中文数据池
# ============================================================

SURNAMES = [
    "王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴",
    "徐", "孙", "胡", "朱", "高", "林", "何", "郭", "马", "罗",
    "梁", "宋", "郑", "谢", "韩", "唐", "冯", "于", "董", "萧",
    "程", "曹", "袁", "邓", "许", "傅", "沈", "曾", "彭", "吕",
    "苏", "卢", "蒋", "蔡", "贾", "丁", "魏", "薛", "叶", "阎",
    "余", "潘", "杜", "戴", "夏", "钟", "汪", "田", "任", "姜",
    "范", "方", "石", "姚", "谭", "廖", "邹", "熊", "金", "陆",
    "郝", "孔", "白", "崔", "康", "毛", "邱", "秦", "江", "史",
]

GIVEN_NAMES = [
    "伟", "芳", "娜", "秀英", "敏", "静", "丽", "强", "磊", "洋",
    "艳", "勇", "军", "杰", "娟", "涛", "明", "超", "秀兰", "霞",
    "平", "刚", "桂英", "文", "华", "慧", "建华", "玉兰", "建国", "建军",
    "丹", "志强", "秀珍", "建平", "美玲", "雪梅", "海燕", "晓明", "小红", "志伟",
    "雅琴", "佳", "俊", "婷", "鑫", "浩", "宇", "轩", "晨", "瑞",
    "思远", "子涵", "梓涵", "欣怡", "子轩", "浩然", "皓轩", "宇轩", "梓萱", "语桐",
    "雨泽", "博远", "博文", "天佑", "思源", "若曦", "诗涵", "梦琪", "一诺", "依诺",
]

PROVINCES_CITIES = {
    "北京市": ["东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区", "通州区", "大兴区"],
    "上海市": ["黄浦区", "徐汇区", "长宁区", "静安区", "普陀区", "虹口区", "杨浦区", "浦东新区"],
    "广东省": ["广州市", "深圳市", "珠海市", "汕头市", "佛山市", "东莞市", "中山市", "惠州市"],
    "浙江省": ["杭州市", "宁波市", "温州市", "嘉兴市", "湖州市", "绍兴市", "金华市", "台州市"],
    "江苏省": ["南京市", "苏州市", "无锡市", "常州市", "南通市", "扬州市", "徐州市", "盐城市"],
    "四川省": ["成都市", "绵阳市", "德阳市", "宜宾市", "泸州市", "达州市", "南充市", "乐山市"],
    "湖北省": ["武汉市", "宜昌市", "襄阳市", "荆州市", "黄冈市", "十堰市", "孝感市", "咸宁市"],
    "湖南省": ["长沙市", "株洲市", "湘潭市", "衡阳市", "岳阳市", "常德市", "郴州市", "邵阳市"],
    "山东省": ["济南市", "青岛市", "烟台市", "潍坊市", "临沂市", "济宁市", "淄博市", "威海市"],
    "河南省": ["郑州市", "洛阳市", "开封市", "南阳市", "新乡市", "安阳市", "许昌市", "焦作市"],
    "福建省": ["福州市", "厦门市", "泉州市", "漳州市", "莆田市", "宁德市", "龙岩市", "三明市"],
    "陕西省": ["西安市", "咸阳市", "宝鸡市", "渭南市", "汉中市", "延安市", "安康市", "商洛市"],
    "重庆市": ["渝中区", "江北区", "南岸区", "沙坪坝区", "九龙坡区", "渝北区", "巴南区", "北碚区"],
    "天津市": ["和平区", "河东区", "河西区", "南开区", "河北区", "红桥区", "滨海新区", "东丽区"],
}

COMPANY_PREFIXES = [
    "华", "中", "国", "新", "大", "天", "金", "恒", "鑫", "万",
    "龙", "盛", "宏", "博", "瑞", "信", "诚", "德", "泰", "安",
]
COMPANY_SUFFIXES_WORD = [
    "达", "通", "创", "联", "远", "科", "和", "嘉", "源", "辉",
]
COMPANY_TYPES = ["科技有限公司", "信息技术有限公司", "网络科技有限公司", "贸易有限公司",
                 "实业有限公司", "投资有限公司", "咨询有限公司", "传媒有限公司",
                 "电子商务有限公司", "数据服务有限公司", "智能科技有限公司", "生物科技有限公司"]

POSITIONS = [
    "软件工程师", "产品经理", "项目经理", "UI设计师", "数据分析师",
    "运营专员", "市场经理", "销售总监", "人事主管", "财务经理",
    "前端开发", "后端开发", "测试工程师", "运维工程师", "架构师",
    "技术总监", "CTO", "CEO", "COO", "行政助理",
]

INDUSTRIES = [
    "互联网", "金融", "教育", "医疗", "电商", "游戏", "人工智能",
    "新能源", "房地产", "餐饮", "物流", "汽车", "制造业", "传媒",
    "旅游", "农业", "环保", "法律", "咨询", "广告",
]

STREETS = ["中山路", "解放路", "人民路", "建设路", "和平路", "长江路",
           "文化路", "新华路", "光明路", "幸福路", "学院路", "科技路"]

PHONE_PREFIXES = [
    "130", "131", "132", "133", "134", "135", "136", "137", "138", "139",
    "150", "151", "152", "153", "155", "156", "157", "158", "159",
    "170", "171", "172", "173", "175", "176", "177", "178",
    "180", "181", "182", "183", "184", "185", "186", "187", "188", "189",
    "191", "198", "199",
]

BANK_PREFIXES = [
    ("622202", "工商银行"), ("622848", "农业银行"), ("622700", "建设银行"),
    ("621661", "中国银行"), ("622588", "招商银行"), ("621286", "交通银行"),
    ("622622", "民生银行"), ("622521", "兴业银行"), ("622908", "兴业银行"),
    ("621483", "浦发银行"), ("622576", "华夏银行"), ("621626", "邮储银行"),
]

CAR_CITY_CODES = [
    "京A", "京B", "沪A", "沪B", "粤A", "粤B", "浙A", "浙B",
    "苏A", "苏B", "川A", "渝A", "鄂A", "湘A", "鲁A", "豫A",
    "闽A", "陕A", "津A", "冀A",
]

EMAIL_DOMAINS = [
    "qq.com", "163.com", "126.com", "sina.com", "gmail.com",
    "outlook.com", "foxmail.com", "hotmail.com", "yeah.net", "139.com",
]

# ============================================================
# 生成函数
# ============================================================

def gen_name():
    surname = random.choice(SURNAMES)
    given = random.choice(GIVEN_NAMES)
    return surname + given


def gen_gender():
    return random.choice(["男", "女"])


def gen_age(min_age=18, max_age=65):
    return random.randint(min_age, max_age)


def _safe_replace_year(dt, year):
    """安全替换年份：闰年 2 月 29 日在目标年份不合法时回退到 2 月 28 日"""
    try:
        return dt.replace(year=year)
    except ValueError:
        return dt.replace(year=year, day=28)


def gen_birthday(min_age=18, max_age=65):
    today = datetime.now()
    max_date = _safe_replace_year(today, today.year - min_age)
    min_date = _safe_replace_year(today, today.year - max_age)
    delta = (max_date - min_date).days
    rand_days = random.randint(0, delta)
    return (min_date + timedelta(days=rand_days)).strftime("%Y-%m-%d")


def gen_phone():
    prefix = random.choice(PHONE_PREFIXES)
    suffix = ''.join([str(random.randint(0, 9)) for _ in range(8)])
    return prefix + suffix


def gen_id_number():
    """生成符合校验规则的身份证号"""
    area_codes = [
        "110101", "110102", "310101", "310104", "440103", "440305",
        "330102", "330106", "320102", "320505", "510104", "510107",
        "420102", "420106", "430102", "430104", "370102", "370202",
    ]
    area = random.choice(area_codes)
    birthday = gen_birthday(min_age=18, max_age=70)
    date_str = birthday.replace("-", "")
    seq = ''.join([str(random.randint(0, 9)) for _ in range(3)])
    base = area + date_str + seq
    # 计算校验位
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check_chars = "10X98765432"
    total = sum(int(base[i]) * weights[i] for i in range(17))
    check = check_chars[total % 11]
    return base + check


def gen_email():
    username = ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(6, 12)))
    domain = random.choice(EMAIL_DOMAINS)
    return f"{username}@{domain}"


def gen_address():
    province_cities = random.choice(list(PROVINCES_CITIES.items()))
    province = province_cities[0]
    city = random.choice(province_cities[1])
    if "市" in province and province == city.replace("市", ""):
        district = random.choice(province_cities[1])
        detail = f"{province}{district}"
    else:
        district = random.choice(province_cities[1])
        detail = f"{province}{city}{district}"
    street = random.choice(STREETS)
    number = random.randint(1, 200)
    building = random.randint(1, 30)
    room = random.randint(101, 2503)
    return f"{detail}{street}{number}号{building}栋{room}室"


def gen_province():
    return random.choice(list(PROVINCES_CITIES.keys()))


def gen_city():
    province = random.choice(list(PROVINCES_CITIES.keys()))
    city = random.choice(PROVINCES_CITIES[province])
    return city


def gen_postcode():
    return f"{random.randint(100000, 699999)}"


def gen_longitude():
    return round(random.uniform(73.0, 135.0), 6)


def gen_latitude():
    return round(random.uniform(18.0, 53.0), 6)


def gen_company():
    prefix = random.choice(COMPANY_PREFIXES)
    suffix = random.choice(COMPANY_SUFFIXES_WORD)
    c_type = random.choice(COMPANY_TYPES)
    return prefix + suffix + c_type


def gen_position():
    return random.choice(POSITIONS)


def gen_industry():
    return random.choice(INDUSTRIES)


def gen_bank_card():
    """生成符合Luhn校验的银行卡号"""
    prefix, bank = random.choice(BANK_PREFIXES)
    remaining = 16 - len(prefix)
    base = prefix + ''.join([str(random.randint(0, 9)) for _ in range(remaining - 1)])
    # Luhn校验
    digits = [int(d) for d in base]
    for i in range(len(digits) - 1, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] -= 9
    total = sum(digits)
    check = (10 - total % 10) % 10
    return base + str(check)


def gen_credit_code():
    """统一社会信用代码(18位)"""
    chars = "0123456789ABCDEFGHJKLMNPQRTUWXY"
    base = ''.join(random.choices(chars, k=17))
    weights = [1, 3, 9, 27, 19, 3, 15, 19, 27, 19, 15, 3, 13, 17, 9, 7, 3]
    total = sum(chars.index(base[i]) * weights[i] for i in range(17))
    check = chars[(31 - total % 31) % 31]
    return base + check


def gen_ip():
    return f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"


def gen_mac():
    return ':'.join([f"{random.randint(0,255):02X}" for _ in range(6)])


def gen_url():
    words = random.choices(string.ascii_lowercase, k=random.randint(5, 10))
    domain = ''.join(words)
    tld = random.choice(["com", "cn", "net", "org", "io"])
    return f"https://www.{domain}.{tld}"


def gen_username():
    prefixes = ["user", "test", "admin", "demo", "dev"]
    return random.choice(prefixes) + str(random.randint(100, 99999))


def gen_password():
    chars = string.ascii_letters + string.digits + "!@#$%"
    return ''.join(random.choices(chars, k=random.randint(8, 16)))


def gen_date(days_back=365*3):
    today = datetime.now()
    rand_days = random.randint(0, days_back)
    return (today - timedelta(days=rand_days)).strftime("%Y-%m-%d")


def gen_datetime(days_back=365*3):
    today = datetime.now()
    rand_days = random.randint(0, days_back)
    rand_seconds = random.randint(0, 86400)
    dt = today - timedelta(days=rand_days, seconds=rand_seconds)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def gen_timestamp(days_back=365*3):
    today = datetime.now()
    rand_days = random.randint(0, days_back)
    rand_seconds = random.randint(0, 86400)
    dt = today - timedelta(days=rand_days, seconds=rand_seconds)
    return int(dt.timestamp())


def gen_int(min_val=1, max_val=10000):
    return random.randint(min_val, max_val)


def gen_float(min_val=0, max_val=10000, decimals=2):
    return round(random.uniform(min_val, max_val), decimals)


def gen_amount(min_val=1, max_val=99999):
    return round(random.uniform(min_val, max_val), 2)


def gen_percentage():
    return round(random.uniform(0, 100), 2)


def gen_uuid():
    import uuid
    return str(uuid.uuid4())


def gen_order_id(prefix="DD"):
    date_part = datetime.now().strftime("%Y%m%d%H%M")
    rand_part = ''.join(random.choices(string.digits, k=6))
    return f"{prefix}{date_part}{rand_part}"


def gen_car_plate():
    city_code = random.choice(CAR_CITY_CODES)
    # 新能源车牌可能更长
    if random.random() < 0.3:
        # 新能源
        letter = random.choice("DF")
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
        return city_code + letter + suffix
    else:
        letter = random.choice(string.ascii_uppercase.replace("I", "").replace("O", ""))
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        return city_code + letter + suffix


def gen_isbn():
    """生成ISBN-13"""
    prefix = "978"
    group = random.choice(["7", "1", "0"])
    publisher = ''.join(random.choices(string.digits, k=random.randint(3, 5)))
    fill_len = 12 - len(prefix) - len(group) - len(publisher)
    title = ''.join(random.choices(string.digits, k=max(1, fill_len)))
    base = prefix + group + publisher + title
    base = base[:12]
    # ISBN-13 校验
    digits = [int(d) for d in base]
    total = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    check = (10 - total % 10) % 10
    return base + str(check)


def gen_paragraph():
    try:
        from faker import Faker
        f = Faker('zh_CN')
        return f.paragraph()
    except ImportError:
        return "这是一段测试文本，用于填充数据内容。实际使用请安装faker库以获得更好的效果。"


def gen_sentence():
    try:
        from faker import Faker
        f = Faker('zh_CN')
        return f.sentence()
    except ImportError:
        return "测试语句生成。"


def gen_word():
    try:
        from faker import Faker
        f = Faker('zh_CN')
        return f.word()
    except ImportError:
        return "测试"


def gen_avatar():
    uid = random.randint(1, 9999)
    return f"https://i.pravatar.cc/150?u={uid}"


# ============================================================
# 字段类型映射
# ============================================================

FIELD_GENERATORS = {
    "姓名": gen_name, "name": gen_name,
    "性别": gen_gender, "gender": gen_gender,
    "年龄": gen_age, "age": gen_age,
    "生日": gen_birthday, "birthday": gen_birthday,
    "手机号": gen_phone, "phone": gen_phone,
    "身份证号": gen_id_number, "id_number": gen_id_number, "idcard": gen_id_number,
    "邮箱": gen_email, "email": gen_email,
    "地址": gen_address, "address": gen_address,
    "省份": gen_province, "province": gen_province,
    "城市": gen_city, "city": gen_city,
    "邮编": gen_postcode, "postcode": gen_postcode,
    "经度": gen_longitude, "longitude": gen_longitude,
    "纬度": gen_latitude, "latitude": gen_latitude,
    "公司": gen_company, "company": gen_company,
    "职位": gen_position, "position": gen_position,
    "行业": gen_industry, "industry": gen_industry,
    "银行卡号": gen_bank_card, "bank_card": gen_bank_card,
    "信用代码": gen_credit_code, "credit_code": gen_credit_code,
    "IP": gen_ip, "ip": gen_ip, "ip_address": gen_ip,
    "MAC地址": gen_mac, "mac": gen_mac,
    "URL": gen_url, "url": gen_url,
    "用户名": gen_username, "username": gen_username,
    "密码": gen_password, "password": gen_password,
    "日期": gen_date, "date": gen_date,
    "时间": gen_datetime, "datetime": gen_datetime,
    "时间戳": gen_timestamp, "timestamp": gen_timestamp,
    "整数": gen_int, "integer": gen_int,
    "小数": gen_float, "float": gen_float,
    "金额": gen_amount, "amount": gen_amount, "money": gen_amount,
    "百分比": gen_percentage, "percentage": gen_percentage,
    "UUID": gen_uuid, "uuid": gen_uuid,
    "订单号": lambda: gen_order_id(), "order_id": lambda: gen_order_id(),
    "车牌号": gen_car_plate, "car_plate": gen_car_plate,
    "ISBN": gen_isbn, "isbn": gen_isbn,
    "段落": gen_paragraph, "paragraph": gen_paragraph,
    "句子": gen_sentence, "sentence": gen_sentence,
    "词语": gen_word, "word": gen_word,
    "头像": gen_avatar, "avatar": gen_avatar,
}

DEFAULT_TABLE_FIELDS = ["姓名", "性别", "手机号", "身份证号", "邮箱", "地址", "公司", "职位", "银行卡号", "日期", "金额"]

# ============================================================
# 命令处理
# ============================================================

def cmd_table(args):
    """生成表格数据"""
    rows = args.rows
    fields_str = args.fields
    output = args.output or "reports/测试数据"
    fmt = args.format or "xlsx"

    if fields_str:
        fields = [f.strip() for f in fields_str.split(",")]
    else:
        fields = DEFAULT_TABLE_FIELDS

    print(f"生成表格数据: {rows} 行 x {len(fields)} 列")
    print(f"字段: {', '.join(fields)}")

    data = []
    for i in range(rows):
        row = {}
        for field in fields:
            gen_func = FIELD_GENERATORS.get(field)
            if gen_func:
                row[field] = gen_func()
            else:
                row[field] = f"未知字段_{i}"
        data.append(row)
        if (i + 1) % 100 == 0:
            print(f"  已生成 {i+1}/{rows} 行...")

    save_data(data, output, fmt)
    print(f"\n✅ 生成完成: {rows} 行数据")
    print(f"📁 保存至: {get_filepath(output, fmt)}")


def cmd_gen(args):
    """生成单类数据"""
    data_type = args.type
    count = args.count
    output = args.output
    fmt = args.format or "txt"

    gen_func = FIELD_GENERATORS.get(data_type)
    if not gen_func:
        print(f"❌ 不支持的数据类型: {data_type}")
        print(f"   支持的类型: {', '.join(sorted(set(FIELD_GENERATORS.keys())))}")
        return 1

    print(f"生成 {data_type}: {count} 条")
    results = [gen_func() for _ in range(count)]

    data = [{data_type: v} for v in results]

    if output:
        save_data(data, output, fmt)
        print(f"\n✅ 生成完成: {count} 条 {data_type}")
        print(f"📁 保存至: {get_filepath(output, fmt)}")
    else:
        # 直接打印到控制台
        for v in results:
            print(v)

    return 0


def cmd_api(args):
    """生成接口报文数据"""
    name = args.name or "数据"
    count = args.count
    output = args.output or f"reports/{name}"
    fmt = "json"

    print(f"生成接口报文: {name}, {count} 条")

    records = []
    for i in range(count):
        record = {}
        for field in DEFAULT_TABLE_FIELDS:
            gen_func = FIELD_GENERATORS.get(field)
            if gen_func:
                record[field] = gen_func()
        records.append(record)

    api_data = {
        "code": 200,
        "message": "success",
        "data": {
            "total": count,
            "page": 1,
            "pageSize": count,
            "list": records
        },
        "timestamp": gen_timestamp()
    }

    filepath = get_filepath(output, fmt)
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(api_data, f, ensure_ascii=False, indent=2)

    size_kb = os.path.getsize(filepath) / 1024
    print(f"\n✅ 生成完成: {count} 条接口数据")
    print(f"📁 保存至: {filepath} ({size_kb:.1f} KB)")


def cmd_template(args):
    """通过模板JSON生成数据"""
    config_path = args.config
    rows = args.rows
    output = args.output
    fmt = args.format or "xlsx"

    if not os.path.exists(config_path):
        print(f"❌ 配置文件不存在: {config_path}")
        return 1

    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    name = config.get("name", "模板数据")
    fields_config = config.get("fields", {})

    print(f"按模板生成: {name}, {rows} 行")

    data = []
    for i in range(rows):
        row = {}
        for field_name, field_rule in fields_config.items():
            row[field_name] = generate_by_rule(field_rule, i)
        data.append(row)

    out_path = output or f"reports/{name}"
    save_data(data, out_path, fmt)
    print(f"\n✅ 生成完成: {rows} 行模板数据")
    print(f"📁 保存至: {get_filepath(out_path, fmt)}")


def generate_by_rule(rule, index):
    """根据规则生成单个值"""
    rtype = rule.get("type", "integer")

    if rtype in FIELD_GENERATORS:
        return FIELD_GENERATORS[rtype]()

    # 自定义类型
    if rtype == "order_id":
        prefix = rule.get("prefix", "DD")
        return gen_order_id(prefix)
    elif rtype == "choice":
        options = rule.get("options", [])
        return random.choice(options) if options else ""
    elif rtype == "integer":
        min_val = rule.get("min", 1)
        max_val = rule.get("max", 10000)
        return random.randint(min_val, max_val)
    elif rtype == "amount":
        min_val = rule.get("min", 1)
        max_val = rule.get("max", 99999)
        return round(random.uniform(min_val, max_val), 2)
    elif rtype == "float":
        min_val = rule.get("min", 0)
        max_val = rule.get("max", 10000)
        decimals = rule.get("decimals", 2)
        return round(random.uniform(min_val, max_val), decimals)
    elif rtype == "fixed":
        return rule.get("value", "")
    elif rtype == "sequence":
        prefix = rule.get("prefix", "")
        start = rule.get("start", 1)
        pad = rule.get("pad", 0)
        val = start + index
        if pad:
            return f"{prefix}{str(val).zfill(pad)}"
        return f"{prefix}{val}"
    elif rtype == "regex":
        pattern = rule.get("pattern", r"\w{8}")
        try:
            import rstr
            return rstr.xeger(pattern)
        except ImportError:
            # 简单正则生成回退
            return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    elif rtype == "date_range":
        start = rule.get("start", "2020-01-01")
        end = rule.get("end", "2025-12-31")
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
        delta = (e - s).days
        return (s + timedelta(days=random.randint(0, delta))).strftime("%Y-%m-%d")
    else:
        return f"未知类型_{rtype}"


# ============================================================
# 保存函数
# ============================================================

def get_filepath(output, fmt):
    """获取输出文件路径"""
    ext_map = {"xlsx": ".xlsx", "csv": ".csv", "json": ".json", "txt": ".txt"}
    ext = ext_map.get(fmt, ".xlsx")
    if not output.endswith(ext):
        output = output + ext
    return output


def save_data(data, output, fmt):
    """保存数据到文件"""
    filepath = get_filepath(output, fmt)
    dir_path = os.path.dirname(filepath)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    if fmt == "xlsx":
        save_xlsx(data, filepath)
    elif fmt == "csv":
        save_csv(data, filepath)
    elif fmt == "json":
        save_json(data, filepath)
    elif fmt == "txt":
        save_txt_file(data, filepath)
    else:
        save_xlsx(data, filepath)


def save_xlsx(data, filepath):
    try:
        import pandas as pd
        df = pd.DataFrame(data)
        df.to_excel(filepath, index=False, engine="openpyxl")
    except ImportError:
        # 回退到csv
        csv_path = filepath.replace(".xlsx", ".csv")
        save_csv(data, csv_path)
        print(f"  ⚠️ 未安装pandas/openpyxl，已改存CSV: {csv_path}")


def save_csv(data, filepath):
    if not data:
        return
    headers = list(data[0].keys())
    with open(filepath, 'w', encoding='utf-8-sig', errors='replace', newline='') as f:
        import csv
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in data:
            writer.writerow([str(row.get(h, "")) for h in headers])


def save_json(data, filepath):
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_txt_file(data, filepath):
    if not data:
        return
    headers = list(data[0].keys())
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(" | ".join(headers) + "\n")
        f.write("-" * 80 + "\n")
        for row in data:
            values = [str(row.get(h, "")) for h in headers]
            f.write(" | ".join(values) + "\n")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='数据生成器 - 批量生成测试数据')
    subparsers = parser.add_subparsers(dest='command', help='子命令')

    # table 命令
    p_table = subparsers.add_parser('table', help='生成表格数据')
    p_table.add_argument('--rows', type=int, default=100, help='行数 (默认100)')
    p_table.add_argument('--fields', default='', help='字段列表，逗号分隔')
    p_table.add_argument('--output', default='', help='输出路径(不含扩展名)')
    p_table.add_argument('--format', default='xlsx', choices=['xlsx', 'csv', 'json', 'txt'], help='输出格式')

    # gen 命令
    p_gen = subparsers.add_parser('gen', help='生成单类数据')
    p_gen.add_argument('--type', required=True, help='数据类型')
    p_gen.add_argument('--count', type=int, default=10, help='数量')
    p_gen.add_argument('--output', default='', help='输出路径(不含扩展名)')
    p_gen.add_argument('--format', default='txt', choices=['xlsx', 'csv', 'json', 'txt'], help='输出格式')

    # api 命令
    p_api = subparsers.add_parser('api', help='生成接口报文')
    p_api.add_argument('--name', default='', help='接口名称')
    p_api.add_argument('--count', type=int, default=20, help='数据条数')
    p_api.add_argument('--output', default='', help='输出路径(不含扩展名)')

    # template 命令
    p_tpl = subparsers.add_parser('template', help='按模板生成')
    p_tpl.add_argument('--config', required=True, help='模板JSON文件路径')
    p_tpl.add_argument('--rows', type=int, default=100, help='行数')
    p_tpl.add_argument('--output', default='', help='输出路径(不含扩展名)')
    p_tpl.add_argument('--format', default='xlsx', choices=['xlsx', 'csv', 'json', 'txt'], help='输出格式')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    print(f"{'='*50}")
    print(f"  数据生成器")
    print(f"{'='*50}\n")

    if args.command == 'table':
        return cmd_table(args)
    elif args.command == 'gen':
        return cmd_gen(args)
    elif args.command == 'api':
        return cmd_api(args)
    elif args.command == 'template':
        return cmd_template(args)


if __name__ == '__main__':
    sys.exit(main() or 0)
