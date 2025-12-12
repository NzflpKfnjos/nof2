提示词必须自己重写，基础代码是提供学习魔改。

新增加了止盈止损条件单
动态调整止盈止损
反手开单

其它开单金额,杠杆设置都在提示词prompt.txt文件

💰 账户资金计算规则
**重要**：`position_size` 是**账户总权益**。

**计算步骤**：
1. **账户总权益** = 账户余额 + 当前未实现盈亏
2. **名义价值** = 账户总权益 × 2
3. **position_size** = 名义价值（JSON中填写此值）
4. **实际开单金额** = position_size

**示例**：账户总权益 $500，杠杆 2x
- 账户总权益 = $500
- position_size = $500 × 2 = **$1000** ← JSON填此值

开仓杠杆限制：
- BTC/ETH 主流币种最小杠杆下限:5x 最大杠杆上限是10x
- 其它山寨币种最小杠杆下限:1x 最大杠杆上限是3x

重点:这部分内容一定不能删除,其它的随便改.

表单要修改配置文件config.py填入您的币安公私钥就可以了

手机安装所需要的库，其他不懂的就问ai

自己去研究吧

pip install -r requirements.txt

启动:python3 main.py

启动前端:python3 api_history.py

前端访问地址：http://127.0.0.1:8600

docker rm -f
docker rmi -f
docker logs -f nofx

docker compose up -d --build


<img width="1918" height="903" alt="image" src="https://github.com/user-attachments/assets/0824bffa-b8c3-4f63-add3-acff1725dba9" />

<img width="1918" height="903" alt="image" src="https://github.com/user-attachments/assets/728faf5f-2767-4303-905a-f52dcf46b905" />

<img width="1918" height="903" alt="image" src="https://github.com/user-attachments/assets/9f1c88d1-c787-482c-9016-69ebf0b468ce" />

<img width="1918" height="903" alt="image" src="https://github.com/user-attachments/assets/70422b46-50ff-4477-badd-ec932f943837" />



