# Jarvis

Plataforma de monitorização e análise de infraestrutura com raciocínio por IA — agente Windows opcional, execução remota sem agente (agentless) via Kerberos, motor de automação orientado a linguagem natural, e um ciclo de conhecimento aprovado por humanos.

Este repositório contém o código-fonte genérico do projecto — limpo de qualquer configuração, credencial ou nome de infraestrutura específicos de uma instalação particular. Todos os valores de ambiente necessários estão documentados nos ficheiros `.env.example` / `*.conf.example`.

## Estrutura

- `agent/` — serviço Windows opcional, instalado no endpoint monitorizado (telemetria, inventário, execução de comandos).
- `jarvis_center/` — processo central: ingestão, motores de análise, execução agentless, motor de automação (Lachesis), integrações genéricas (Clotho), raciocínio conversacional.
- `jarvis-ctl/` — CLI de administração/diagnóstico.
- `docs/patent/` — memória descritiva técnica e diagramas dos mecanismos submetidos a pedido de patente.

## Configuração

Copiar cada `*.env.example` para `.env` (ou `*.conf.example`/`*.conf.dist` para `.conf`) no directório correspondente, e preencher os valores próprios da instalação — nenhum valor por omissão neste repositório aponta para infraestrutura real.

## Licença

A definir pelo autor.
