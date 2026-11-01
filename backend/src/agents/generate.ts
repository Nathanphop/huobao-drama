import type { RequestContext } from '@mastra/core/request-context'
import { mastra } from '../mastra/index.js'
import { getConfigById, getTextConfig } from '../services/ai.js'
import { runCodexAgent } from '../services/codex-companion.js'
import { AGENT_TOOLS, resolveAgentInstructions } from './index.js'

interface AgentMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

interface GenerateConfiguredAgentOptions {
  requestContext: RequestContext<any>
  maxSteps?: number
  onStepFinish?: (step: any) => void
}

function codexMessage(messages: AgentMessage[]): string {
  return messages
    .filter(message => message.role !== 'system')
    .map(message => `${message.role === 'assistant' ? 'Assistant' : 'User'}:\n${message.content}`)
    .join('\n\n')
}

/** Use Mastra for API-key providers and local Codex app-server for the codex provider. */
export async function generateConfiguredAgent(
  agentType: string,
  messages: AgentMessage[],
  options: GenerateConfiguredAgentOptions,
): Promise<any> {
  const textConfigId = options.requestContext.get('textConfigId' as never) as number | undefined
  const modelOverride = options.requestContext.get('modelOverride' as never) as string | undefined
  const requestedConfig = textConfigId ? await getConfigById(textConfigId) : null
  const textConfig = requestedConfig || await getTextConfig()

  if (textConfig.provider.toLowerCase() === 'codex') {
    return runCodexAgent({
      message: codexMessage(messages),
      instructions: await resolveAgentInstructions(agentType, options.requestContext),
      model: modelOverride || textConfig.model,
      tools: AGENT_TOOLS[agentType],
      requestContext: options.requestContext,
    })
  }

  const agent = mastra.getAgent(agentType)
  if (!agent) throw new Error(`Agent not found: ${agentType}`)
  return agent.generate(messages as any, {
    maxSteps: options.maxSteps,
    requestContext: options.requestContext,
    onStepFinish: options.onStepFinish,
  })
}
