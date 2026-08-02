import datetime

def calc_target_mode(now):
    weekday = now.weekday()
    minutes = now.hour * 60 + now.minute
    DS_START = 18 * 60
    DS_END_NEXT_DAY = 1 * 60 + 30

    if weekday == 5 or weekday == 6:
        return 'deepseek'
    elif weekday == 4:  # Fri
        if minutes >= DS_START:
            return 'deepseek'
        elif minutes <= DS_END_NEXT_DAY:
            return 'deepseek'
        return 'ollama'
    elif weekday == 0:  # Mon
        if minutes <= DS_END_NEXT_DAY:
            return 'deepseek'
        elif minutes >= DS_START:
            return 'deepseek'
        return 'ollama'
    else:  # Tue, Wed, Thu
        if minutes <= DS_END_NEXT_DAY:
            return 'deepseek'
        elif minutes >= DS_START:
            return 'deepseek'
        return 'ollama'

tests = [
    ('Mon 10:00', datetime.datetime(2026,7,20,10,0), 'ollama'),
    ('Mon 18:30', datetime.datetime(2026,7,20,18,30), 'deepseek'),
    ('Mon 23:00', datetime.datetime(2026,7,20,23,0), 'deepseek'),
    ('Tue 01:00', datetime.datetime(2026,7,21,1,0), 'deepseek'),
    ('Tue 01:31', datetime.datetime(2026,7,21,1,31), 'ollama'),
    ('Tue 10:00', datetime.datetime(2026,7,21,10,0), 'ollama'),
    ('Tue 18:00', datetime.datetime(2026,7,21,18,0), 'deepseek'),
    ('Wed 00:30', datetime.datetime(2026,7,22,0,30), 'deepseek'),
    ('Thu 12:00', datetime.datetime(2026,7,23,12,0), 'ollama'),
    ('Thu 18:00', datetime.datetime(2026,7,23,18,0), 'deepseek'),
    ('Fri 01:15', datetime.datetime(2026,7,24,1,15), 'deepseek'),
    ('Fri 01:31', datetime.datetime(2026,7,24,1,31), 'ollama'),
    ('Fri 12:00', datetime.datetime(2026,7,24,12,0), 'ollama'),
    ('Fri 18:00', datetime.datetime(2026,7,24,18,0), 'deepseek'),
    ('Sat 10:00', datetime.datetime(2026,7,25,10,0), 'deepseek'),
    ('Sun 23:00', datetime.datetime(2026,7,26,23,0), 'deepseek'),
    ('Mon 01:15', datetime.datetime(2026,7,27,1,15), 'deepseek'),
    ('Mon 01:31', datetime.datetime(2026,7,27,1,31), 'ollama'),
    ('Mon 08:00', datetime.datetime(2026,7,27,8,0), 'ollama'),
]

all_ok = True
for label, dt, expected in tests:
    result = calc_target_mode(dt)
    status = 'OK' if result == expected else 'FAIL'
    if result != expected:
        all_ok = False
    print(f'[{status}] {label} ({dt.strftime("%a %H:%M")}) -> {result} (want {expected})')

print()
print('ALL PASSED' if all_ok else 'SOME FAILED')
