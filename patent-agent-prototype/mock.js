/* ============================================================
   Patent-Agent · Mock 数据单一来源（所有页面只从 DB 读取，禁止散落硬编码）
   ============================================================ */
var DB = {

  /* ---------- 案件列表（工作台） ---------- */
  cases: [
    { id: 'CN2026-0881', title: '一种基于复合相变材料的动力电池热管理系统', client: '星驰新能源科技', field: 'H01M 10/6566', status: '检索中', progress: 62, updated: '今天 09:41', agent: 'Agent Loop 运行中 · 第 3/5 步' },
    { id: 'CN2026-0879', title: '车规级激光雷达的点云去噪方法及装置', client: '锐驰智驾', field: 'G01S 17/88', status: '待审批', progress: 30, updated: '今天 08:15', agent: '检索式已生成，等待代理师审批' },
    { id: 'CN2026-0877', title: '一种钙钛矿太阳能电池的界面钝化结构', client: '曜光材料', field: 'H10K 30/50', status: '已完成', progress: 100, updated: '昨天 17:02', agent: '查新报告已导出 · 3 篇对比文件' },
    { id: 'CN2026-0874', title: '基于联邦学习的医疗影像隐私计算平台', client: '安贞医院联合实验室', field: 'G16H 50/20', status: '对比分析', progress: 78, updated: '昨天 14:30', agent: 'Sub-agent 精读中 · 2/3 篇' },
    { id: 'CN2026-0871', title: '一种低介电常数覆铜板的后固化工艺', client: '华正新材', field: 'C08J 7/04', status: '待审批', progress: 28, updated: '昨天 09:48', agent: '检索式已生成，等待代理师审批' },
    { id: 'CN2026-0868', title: '四旋翼无人机抗风姿态控制方法', client: '云隼航空', field: 'G05D 1/08', status: '已完成', progress: 100, updated: '09-08 16:20', agent: '查新报告已导出 · 结论 A 类' },
    { id: 'CN2026-0865', title: '用于固态电解质膜的交联聚合物粘结剂', client: '清陶能源', field: 'H01M 10/0562', status: '检索中', progress: 45, updated: '09-08 11:05', agent: 'Agentic 循环第 2 次迭代' },
    { id: 'CN2026-0862', title: '手术机器人末端执行器的力反馈装置', client: '精微医疗', field: 'A61B 34/30', status: '对比分析', progress: 82, updated: '09-07 19:44', agent: '特征比对表已生成' },
    { id: 'CN2026-0858', title: '一种便携式土壤重金属快速检测探头', client: '田禾农业科技', field: 'G01N 21/71', status: '已完成', progress: 100, updated: '09-06 15:12', agent: '查新报告已导出 · 1 篇 X 类' },
    { id: 'CN2026-0855', title: '电梯群控系统的节能调度算法', client: '恒立电梯', field: 'B66B 1/06', status: '待解析', progress: 8, updated: '09-06 09:20', agent: '交底书已上传，等待解析' }
  ],

  /* ---------- 当前案件 CN2026-0881 全量数据 ---------- */
  current: {
    id: 'CN2026-0881',
    title: '一种基于复合相变材料的动力电池热管理系统',
    client: '星驰新能源科技',
    clientContact: '周工 · 研发部',
    field: 'H01M 10/6566',
    filed: '2026-08-30',
    updated: '2026-09-10 09:41',

    /* 交底书三要素 */
    disclosure: {
      problem: '现有动力电池多采用单一液冷板方案，快充工况下电芯间温差超过 8℃，局部热点诱发热失控；增大冷却流量又带来约 30% 的附加能耗与续航损失。',
      solution: '以石墨烯复合相变材料（PCM）包覆电芯构成被动均温层，并与微通道液冷板主动冷却耦合：PCM 削平瞬态热峰，微通道带走稳态热负荷；内置分布式温度传感器，热失控预警与冷却策略联动。',
      effect: '模组最大温差由 8.4℃ 降至 2.6℃；1.5C 快充循环 1500 次后容量保持率提升 23%；冷却能耗降低 31%。',
      terms: ['相变材料', '石墨烯复合', '微通道液冷板', '热失控预警', '动力电池', '均温层', '热管理'],
      wordCount: 4860
    },

    /* 自动生成的检索式（HITL 审批对象） */
    query: {
      topic: '动力电池 / 储能电池热管理',
      keywords: ['相变材料', '液冷板', '热管理'],
      synonyms: ['PCM', '相变储热', '液冷', '电池包', '热失控'],
      ipc: ['H01M 10/6566', 'H01M 10/613', 'H05K 7/20'],
      dateFrom: '2018-01-01',
      dateTo: '2026-09-10',
      expr: '(相变材料 OR PCM OR 相变储热) AND (液冷板 OR 液冷) AND (动力电池 OR 电池包 OR 储能电池) AND 热管理',
      lint: '语法校验通过 · 预估召回量级 1.2k–1.8k（可执行）'
    },

    /* 检索结果（两阶段后） */
    hits: [
      { pubNo: 'CN112233445B', title: '一种动力电池复合相变材料热管理结构', assignee: '宁德时代新能源', date: '2021-06-15', score: 0.94, stage: '全文', checked: true },
      { pubNo: 'CN115566778A', title: '电池包液冷板与相变层耦合散热系统', assignee: '比亚迪股份', date: '2023-07-04', score: 0.91, stage: '全文', checked: true },
      { pubNo: 'CN114433221B', title: '基于石墨烯导热片的电池组温差控制装置', assignee: '合肥国轩高科', date: '2022-11-22', score: 0.86, stage: '全文', checked: true },
      { pubNo: 'CN213123456U', title: '一种电动汽车电池包热失控预警装置', assignee: '上汽集团', date: '2021-05-11', score: 0.78, stage: '摘要', checked: false },
      { pubNo: 'CN216873456A', title: '微通道液冷板及其制造方法', assignee: '三花智能控制', date: '2022-04-19', score: 0.75, stage: '摘要', checked: false },
      { pubNo: 'CN111765432B', title: '相变材料包覆电芯的模组封装工艺', assignee: '欣旺达电子', date: '2020-10-27', score: 0.72, stage: '摘要', checked: false },
      { pubNo: 'CN217345678A', title: '储能电池簇浸没式冷却系统', assignee: '阳光电源', date: '2022-09-13', score: 0.64, stage: '摘要', checked: false },
      { pubNo: 'CN113456789B', title: '动力电池快充温度场仿真优化方法', assignee: '奇瑞汽车', date: '2022-01-18', score: 0.58, stage: '摘要', checked: false },
      { pubNo: 'CN218123456A', title: '一种电池热管理控制方法及车辆', assignee: '蔚来汽车', date: '2022-12-06', score: 0.54, stage: '摘要', checked: false },
      { pubNo: 'CN211234567U', title: '电动汽车电池冷却板流道结构', assignee: '银轮机械', date: '2020-11-24', score: 0.49, stage: '摘要', checked: false },
      { pubNo: 'CN216234567A', title: '热失控阻断与排气一体化装置', assignee: '中信国安盟固利', date: '2022-05-30', score: 0.44, stage: '摘要', checked: false },
      { pubNo: 'CN219345678A', title: '电池包保温层结构', assignee: '瑞泰新能源', date: '2023-01-10', score: 0.31, stage: '摘要', checked: false }
    ],

    /* 对比文件（sub-agent 精读产物） */
    priorArt: [
      {
        pubNo: 'CN112233445B', title: '一种动力电池复合相变材料热管理结构',
        assignee: '宁德时代新能源', date: '2021-06-15', grade: 'X', score: 0.94,
        abstract: '公开了以石蜡基复合相变材料包覆电芯、外置液冷板的电池热管理结构，其相变层兼具均温与隔热功能。',
        features: [
          { mine: '石墨烯复合 PCM 包覆电芯构成均温层', theirs: '石蜡基复合 PCM 包覆电芯（导热填料未限定石墨烯）', cite: '[0052][0058]', disclosed: '部分' },
          { mine: '微通道液冷板与 PCM 被动层耦合', theirs: '外置蛇形液冷板，未公开微通道结构与耦合控制策略', cite: '[0061]', disclosed: '否' },
          { mine: '热失控预警与冷却策略联动', theirs: '未涉及', cite: '—', disclosed: '否' }
        ],
        conclusion: '该文件公开了"复合相变材料包覆电芯 + 液冷板"的整体构思，对权利要求 1 的新颖性构成直接威胁（X 类）；石墨烯限定与微通道结构构成可争取的区别特征。'
      },
      {
        pubNo: 'CN115566778A', title: '电池包液冷板与相变层耦合散热系统',
        assignee: '比亚迪股份', date: '2023-07-04', grade: 'Y', score: 0.91,
        abstract: '公开了液冷板与相变层耦合的系统架构，含按温度分区切换冷却模式的控制逻辑。',
        features: [
          { mine: '微通道液冷板与 PCM 被动层耦合', theirs: '液冷板与相变层耦合，且按温度分区切换冷却模式', cite: '[0034][0041]', disclosed: '部分' },
          { mine: '石墨烯复合 PCM 包覆电芯构成均温层', theirs: '相变层为板式布置于模组间，未包覆电芯', cite: '[0028]', disclosed: '否' },
          { mine: '热失控预警与冷却策略联动', theirs: '按温度阈值切换冷却模式，未公开热失控预警联动', cite: '[0047]', disclosed: '部分' }
        ],
        conclusion: '与 CN112233445B 组合后覆盖"耦合控温"的大部分特征，对创造性构成较强威胁（Y 类），组合启示论证将成为撰写重点。'
      },
      {
        pubNo: 'CN114433221B', title: '基于石墨烯导热片的电池组温差控制装置',
        assignee: '合肥国轩高科', date: '2022-11-22', grade: 'A', score: 0.86,
        abstract: '公开了石墨烯导热片平铺于电芯间降低温差的技术方案，属被动均温技术领域背景。',
        features: [
          { mine: '石墨烯复合 PCM 包覆电芯构成均温层', theirs: '石墨烯导热片平铺于电芯间（无相变材料）', cite: '[0019]', disclosed: '否' },
          { mine: '微通道液冷板与 PCM 被动层耦合', theirs: '未涉及液冷结构', cite: '—', disclosed: '否' },
          { mine: '热失控预警与冷却策略联动', theirs: '未涉及', cite: '—', disclosed: '否' }
        ],
        conclusion: '石墨烯用于电池均温的背景技术（A 类），可用于说明材料选取的常规性，撰写时需弱化石墨烯的单独贡献。'
      }
    ],

    /* Agent 运行时事件流（确认检索式后依次追加） */
    events: [
      { ts: '09:41:02', tag: 'loop.gather', msg: '读取交底书 CN2026-0881 → 提取 3 要素 / 7 术语', type: '' },
      { ts: '09:41:05', tag: 'tool.build_query', msg: '检索式 v1 生成：术语规范化 + 同义词 5 项 + IPC 3 项', type: 'tool' },
      { ts: '09:41:05', tag: 'hitl.interrupt', msg: '检索式送审 · interrupt 触发，checkpoint 已持久化（等待代理师）', type: 'hitl' }
    ],
    eventsRun: [
      { ts: '09:43:18', tag: 'hitl.resume', msg: '代理人确认检索式（修改 2 处）→ 从 checkpoint 恢复执行', type: 'hitl' },
      { ts: '09:43:19', tag: 'tool.search', msg: '摘要阶段 · BM25 命中 24 + 向量命中 28 → RRF 融合 41 条', type: 'tool' },
      { ts: '09:43:26', tag: 'tool.search', msg: '全文阶段 · 42 篇候选进入 Cross-Encoder 重排 → Top 5', type: 'tool' },
      { ts: '09:43:31', tag: 'loop.verify', msg: '质量评估：Top5 覆盖 3/4 技术特征，召回不足 → 触发改写', type: 'verify' },
      { ts: '09:43:33', tag: 'agent.rewrite', msg: '检索式 v2：追加（热失控 OR 预警）→ 重检索（迭代 1/5）', type: '' },
      { ts: '09:43:41', tag: 'loop.verify', msg: '质量评估：Top5 覆盖 4/4 技术特征，召回充足 → 通过', type: 'verify' },
      { ts: '09:43:42', tag: 'subagent.spawn', msg: '派生 3 个隔离子代理：逐篇全文精读对比文件', type: 'sub' },
      { ts: '09:44:07', tag: 'subagent.done', msg: 'CN112233445B 精读完成 → 回传特征比对表 + X 类建议', type: 'sub' },
      { ts: '09:44:15', tag: 'subagent.done', msg: 'CN115566778A 精读完成 → 回传特征比对表 + Y 类建议', type: 'sub' },
      { ts: '09:44:23', tag: 'subagent.done', msg: 'CN114433221B 精读完成 → 回传特征比对表 + A 类建议', type: 'sub' },
      { ts: '09:44:25', tag: 'guard.cite', msg: '引用完整性校验：24/24 条结论均挂（专利号+段落号）→ 通过', type: 'verify' }
    ],

    /* 报告元信息 */
    reportMeta: {
      no: 'QX-2026-0881-01',
      date: '2026-09-10',
      searcher: 'Patent-Agent · 初筛',
      reviewer: '代理师（人工复核）',
      db: '中国专利全文数据库（摘要 + 全文两阶段）',
      hitsTotal: 41,
      readDeep: 5
    }
  }
};

/* 流程状态持久化（跨页共享检索式审批结果，演示用） */
var Flow = {
  KEY: 'zxt_flow_cn20260881',
  get: function () {
    try { return JSON.parse(localStorage.getItem(this.KEY)) || { approved: false }; }
    catch (e) { return { approved: false }; }
  },
  set: function (v) { localStorage.setItem(this.KEY, JSON.stringify(v)); }
};
