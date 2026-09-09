// Read-only current-configuration projection, distinct from original proposal and receipts.
export function effectiveScope(overrides = {}) {
  return {
    basis: 'currently_selected_subscription_configuration', selection_basis: 'prospective_public_capture',
    model_provider: 'kimi_subscription', model_name: null, billing_mode: 'existing_subscription',
    model_call_cap: 10, model_call_cap_basis: 'total_agent_invocation_slots_including_unknown_and_review',
    tool_call_cap: 16, tool_call_cap_basis: 'total_new_tool_receipts_per_run',
    capabilities: ['公开来源研究', '必要时安装依赖并隔离试跑公开仓库'],
    external_scope: ['用户本任务问题与经安全校验的公开来源'],
    write_scope: ['Obsidian 研究结论、修订历史与知识目录'],
    exclusions: ['不启用新增付费 API', '不自动部署到用户系统'],
    repository_trial_allowed: true, automatic_knowledge_save_allowed: true,
    observed_model_provider: 'codex_subscription', knowledge_publication_status: 'not_recorded',
    ...overrides,
  };
}
