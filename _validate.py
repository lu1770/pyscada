import yaml

with open('c:/projects/pyscada/daq_config.dtm.yml', 'r', encoding='utf-8') as f:
    c = yaml.safe_load(f)

print('=== 连接配置 ===')
for k, v in c.get('connections', {}).items():
    print(f'  {k}: type={v["type"]}, params={v["params"]}')

print('\n=== 采集任务 (tasks) ===')
for t in c.get('tasks', []):
    print(f'  {t["task_id"]}: addr={t["start_addr"]}, ch={t["channel_name"]}, type={t["connection_type"]}')

print('\n=== 写入任务 (write_tasks) ===')
for t in c.get('write_tasks', []):
    print(f'  {t["task_id"]}: addr={t["start_addr"]}, value={t["value"]}, interval={t["write_interval"]}')

print('\n=== 验证通过 ===')
print(f'连接数: {len(c.get("connections", {}))}')
print(f'采集任务数: {len(c.get("tasks", []))}')
print(f'写入任务数: {len(c.get("write_tasks", []))}')