# frame-pacing

## 题面

换成 120 Hz 屏播放 24 帧片源后，画面开始一顿一顿：所有帧仍按源 PTS 直接下发，音频时钟完全不参与，音画越走越偏；漂移超限时把显示时钟一次性跳到音频时钟，画面先卡住再突然突进；丢帧策略只计算当前帧的延迟，不累计已经形成的迟到量，连续卡顿时仍按单帧判断；重复当前帧时让 pts 跟着前进，时间轴被拉长；卡顿检测比较相邻两帧的间隔，暂停之后反而误判为正常；vsync 对齐一律向下取整，实际误差接近一个完整刷新周期；VFR→CFR 只按平均帧率折算，长片播放时累计漂移越来越明显；多路流共用同一个时钟，A 流卡顿会拖慢 B 流；漂移门限没有参与判断，轻微抖动也触发跳帧；批量调度遇到某一路时钟倒退时只跳过该路，输出里留下倒序时间戳。playback/pacing.py 是核心，tests/test_pacing.py 15个红。要修的是：schedule 以音频时钟为主并在 max_drift_ms 门限内跟随视频，adjust_clock 用 gain 平滑收敛而不是跳变，drop_policy 按累计延迟实现 low_latency 与 smooth 两种模式，repeat_frame 保持 pts 不动并累加 dur，detect_stall 用 now_ms 与最近到达时间判断，vsync_align 对齐到最近刷新点，vfr_to_cfr 把总时长误差控制在一帧以内，pace_batch 让各路独立调度并在任一路时钟倒退时整体回滚。

## 运行方式

```bash
cd D:\gsb-20261119\frame-pacing
python -m pytest tests -q
```

初始状态应恰好输出：

```text
15 failed
```

15 条测试全部因桩函数 `raise NotImplementedError` 而失败；收集、导入和语法检查必须通过，不能出现 `error`。实现后测试应全部通过。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 测试约束

`tests/` 是验收测试，不得修改、删除或新增；修复只能落在 `playback/pacing.py`。所有时间单位为毫秒（ms），断言允许 1 ms 容差。
