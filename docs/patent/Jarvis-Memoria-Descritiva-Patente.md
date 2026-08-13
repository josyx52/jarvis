# Memória Descritiva Técnica — Base para Pedido de Patente

**Documento preparatório**, destinado a servir de base técnica aos advogados/Agente Oficial de Propriedade Industrial (AOPI) na redacção formal do pedido junto do IAPI. Não é o documento final a submeter — a forma legal (reivindicações, resumo, formulário) é da responsabilidade dos advogados. Este documento cobre exclusivamente os três mecanismos técnicos seleccionados como candidatos a protecção. Todo o restante sistema (agente de telemetria instalado, motores de detecção estatística em cascata, interface web, armazenamento) foi deliberadamente excluído por não cumprir, na nossa avaliação preliminar, o critério de actividade inventiva/não-obviedade — são práticas já comuns na indústria de monitorização de infraestrutura.

---

## Mecanismo A — Aplicação estrutural de aprovação humana no despacho de acções de IA

### Campo técnico
Sistemas de execução de acções por agentes de inteligência artificial em infraestrutura de TI, especificamente mecanismos de controlo de autorização que intervêm entre a decisão do modelo de linguagem e a execução real da acção.

### Problema técnico anterior
Em sistemas assistentes de IA com capacidade de executar acções (function calling / tool calling), o controlo de autorização é tipicamente implementado por instrução ao modelo (prompt) — o modelo é instruído a pedir confirmação antes de agir. Esta abordagem depende do comportamento correcto do modelo em cada invocação: uma alucinação, uma instrução adversarial (prompt injection) ou uma simples falha de seguimento de instrução pode resultar na execução de uma acção sem autorização real, sem que exista nenhuma barreira independente do próprio modelo a impedir essa execução.

### Sumário do mecanismo
O sistema implementa uma camada de interceptação, externa ao modelo de linguagem, que actua sobre um conjunto definido de acções classificadas como sujeitas a aprovação. Esta camada intercepta a chamada de ferramenta antes do seu despacho para execução, independentemente da instrução ou do comportamento do modelo nessa invocação específica, e substitui a execução directa por um estado de pendência persistido, com expiração temporal, que só é resolvido por confirmação humana explícita e verificável.

### Descrição detalhada

1. **Classificação estática das acções sujeitas a aprovação.** Um subconjunto fixo de acções disponíveis ao modelo (identificado no sistema por um conjunto nomeado de identificadores) é marcado, na configuração do sistema — não na instrução dada ao modelo — como exigindo aprovação. Esta classificação não pode ser alterada pelo próprio modelo em tempo de execução.

2. **Interceptação anterior ao despacho.** Quando o modelo emite uma chamada de ferramenta correspondente a uma acção classificada como sujeita a aprovação, o sistema intercepta essa chamada num ponto do fluxo de execução anterior a qualquer efeito lateral real — a função que executaria a acção nunca chega a ser invocada nesse momento. Esta interceptação ocorre de forma incondicional para o subconjunto de acções mais sensível (proposta de conhecimento persistente sobre a infraestrutura), independentemente de qualquer outro parâmetro do pedido.

3. **Persistência de estado pendente com expiração.** Em vez de executar, o sistema grava a intenção de acção (parâmetros completos da chamada) associada a um identificador único gerado aleatoriamente, num armazenamento de chave-valor com tempo de vida definido (expiração automática decorrido esse período — na implementação de referência, período da ordem de duas horas). O identificador único funciona como token de resolução de uso único.

4. **Comportamento condicionado ao canal de origem do pedido, não ao conteúdo da acção.** O sistema determina, a partir do canal de comunicação por onde o pedido chegou (interface conversacional directa, integração de mensagens de equipa, ou execução agendada/accionada por evento previamente configurada), qual dos três comportamentos de resolução se aplica:
   - **Bloqueio com pedido de confirmação síncrona** — usado quando o pedido chega por uma interface conversacional directa entre um utilizador humano e o sistema; a chamada de ferramenta é rejeitada com uma instrução para that a confirmação ocorra por via de um mecanismo externo dedicado.
   - **Confirmação assíncrona por texto livre num canal de mensagens** — o estado pendente é associado ao identificador de conversa desse canal, e é resolvido quando uma mensagem subsequente nesse mesmo canal contém um termo de confirmação ou de cancelamento reconhecido.
   - **Execução directa condicionada à pré-autorização do artefacto que originou o pedido** — aplicável apenas quando o pedido não se origina de uma interacção humana em tempo real, mas de uma automação previamente configurada e cuja autorização de execução foi dada no momento da sua criação, não no momento de cada execução individual.

5. **Excepção incondicional para uma subcategoria de acções.** Independentemente do canal determinado no passo 4, uma subcategoria específica de acção (a que resulta na criação de conhecimento persistente sobre a infraestrutura, usado depois para influenciar decisões futuras do sistema) nunca segue o caminho de execução directa — mesmo quando accionada por uma automação pré-autorizada, esta acção é sempre desviada para o estado de pendência do passo 3.

6. **Resolução e persistência definitiva.** Só após recepção de uma confirmação humana explícita — validada contra o identificador único gerado no passo 3, não contra qualquer outro dado do pedido original — o sistema recupera o estado pendente e executa então, e só então, o efeito persistente da acção (execução real, ou escrita definitiva do conhecimento proposto).

### Elementos de novidade
- A classificação de acções sujeitas a aprovação, e a interceptação correspondente, residem inteiramente fora do espaço de decisão do modelo de linguagem — não é possível ao modelo, por instrução, alucinação ou manipulação de prompt, contornar a interceptação, porque esta ocorre num ponto do código anterior a qualquer efeito da chamada.
- A resolução do comportamento de aprovação é função do canal de origem do pedido, e não do conteúdo ou da aparente severidade da acção — um desenho distinto de sistemas que tentam avaliar o "risco" da acção via julgamento do próprio modelo para decidir se pede confirmação.
- Existe uma subcategoria de acção com bloqueio incondicional, que nem sequer beneficia da via de pré-autorização concedida a outras acções no mesmo canal — uma assimetria deliberada entre "agir sobre a infraestrutura" e "gravar conhecimento persistente que vai influenciar decisões futuras".

---

## Mecanismo B — Sistema de execução remota governada com classificação automática de máquina e transporte de bootstrap por polling de saída

### Campo técnico
Sistemas de execução remota de comandos em máquinas Windows geridas por um domínio Active Directory, sem presença de software agente permanentemente instalado nessas máquinas, com autenticação por bilhete de rede nativo do domínio, governação de concorrência e adaptação automática do mecanismo de transporte consoante a topologia de rede.

### Problema técnico anterior
A execução remota tradicional via WinRM (Windows Remote Management) exige uma ligação de rede aberta e mantida entre o sistema orquestrador e a máquina alvo durante toda a duração da operação. Para operações compostas por múltiplos passos sequenciais sobre a mesma máquina, manter essa ligação aberta é frágil (sujeita a interrupção de rede, timeout de sessão, ou fecho antecipado por políticas de segurança) e, nalgumas topologias de rede segmentada, pode não ser sequer possível abrir uma ligação de entrada persistente para a máquina alvo. Adicionalmente, a execução remota concorrente e não governada contra um grande número de máquinas de um domínio pode esgotar recursos de autenticação (número de sessões simultâneas, limites por credencial) sem qualquer mecanismo de protecção. Abordagens alternativas que dependem de credenciais explícitas geridas fora do domínio acrescentam um segredo persistente a proteger e não beneficiam da rotação e da revogação automáticas do bilhete de rede nativo.

### Sumário do mecanismo
O sistema determina automaticamente, por consulta ao directório do domínio, a categoria da máquina alvo, e autentica-se sempre através de um bilhete de rede nativo obtido pela identidade de uma conta de serviço gerida pelo próprio domínio — sem gestão manual de palavra-passe e sem credencial explícita armazenada fora do domínio, para qualquer categoria de máquina. A classificação obtida alimenta o perfil de execução (agrupamento de sessões, parâmetros de tempo-limite) sem alterar o mecanismo de autenticação usado. A execução de comandos é sujeita a um governador de concorrência de múltiplas camadas independentes antes de qualquer ligação ser aberta. Para operações compostas por múltiplos passos sobre a mesma máquina dentro de uma janela temporal, o sistema substitui a manutenção de uma ligação de entrada persistente por um mecanismo de arranque (bootstrap) que lança, através de uma chamada de gestão remota de curta duração, um processo temporário na máquina alvo que estabelece ele próprio ligações de saída periódicas ao orquestrador para obter trabalho e devolver resultados — invertendo a direcção da ligação de rede relativamente ao modelo de execução remota directa.

### Descrição detalhada

1. **Determinação automática de categoria da máquina.** Antes de qualquer tentativa de ligação, o sistema consulta o directório do domínio (unidade organizacional, atributo de sistema operativo) para classificar a máquina alvo, sem que esta classificação seja fornecida manualmente pelo operador em cada pedido.

2. **Autenticação uniforme por bilhete de rede nativo do domínio.** Independentemente da categoria determinada no passo 1, a ligação à máquina alvo usa sempre a identidade de processo de uma conta de serviço gerida pelo domínio — o sistema operativo local resolve e apresenta o bilhete de forma transparente, sem qualquer credencial explícita (utilizador/palavra-passe) armazenada em configuração do orquestrador. A classificação do passo 1 é usada para parametrizar o perfil de execução (agrupamento de sessões, tempos-limite), não para escolher entre mecanismos de autenticação distintos.

3. **Governador de concorrência em camadas independentes.** Antes de qualquer execução, o sistema adquire, em cascata e com um prazo único de espera, quatro contadores de concorrência independentes: um limite global de execuções simultâneas em todo o sistema, um limite por conjunto de credenciais partilhado, um limite por máquina individual, e um limite por operador que originou o pedido. A execução só prossegue quando os quatro limites são simultaneamente respeitados; os contadores são libertados na ordem inversa da aquisição após conclusão.

4. **Detecção automática de operação multi-passo.** O sistema mantém um registo recente de pedidos de execução por máquina; quando detecta um número mínimo de pedidos distintos dirigidos à mesma máquina dentro de uma janela temporal curta, classifica automaticamente a operação em curso como "multi-passo", sem que isso seja declarado explicitamente pelo pedido original.

5. **Transporte de bootstrap por polling de saída, para operações multi-passo.** Em vez de reabrir uma ligação de gestão remota directa a cada passo, o sistema, para operações classificadas no passo 4, invoca uma única vez um mecanismo de criação remota de processo (via interface de gestão do sistema operativo, não via consola/shell remoto interactivo) que lança na máquina alvo um processo autónomo de duração limitada (com tempo de vida definido). Este processo temporário, a partir desse momento, estabelece por iniciativa própria ligações HTTP periódicas de saída ao orquestrador para obter o próximo comando pendente e para devolver o resultado do comando anterior — nenhuma ligação de entrada adicional é aberta para a máquina alvo durante a sequência de passos subsequente à criação inicial do processo.

6. **Fallback determinístico.** Quando a operação não é classificada como multi-passo, ou quando o mecanismo de bootstrap não está disponível, o sistema recorre a uma execução directa síncrona por gestão remota (WinRM), com um mecanismo de verificação por ficheiro de resultado numa localização temporária da máquina alvo e nova consulta periódica, para tolerar a possibilidade de a sessão de gestão remota original expirar antes da conclusão do comando sem que isso implique falha da operação.

7. **Agrupamento de execuções relacionadas por janela de inactividade.** Execuções sucessivas dirigidas à mesma máquina, cujo intervalo entre si não excede uma janela de inactividade configurada, são agrupadas automaticamente numa mesma sessão lógica de investigação, encerrada de forma preguiçosa (na execução seguinte fora da janela) ou por uma verificação periódica de fundo — sem que o operador tenha de delimitar manualmente o início e o fim dessa sessão.

### Elementos de novidade
- A classificação automática da máquina, obtida por consulta ao directório do domínio no momento do pedido, alimenta o perfil de execução (agrupamento de sessões, tempos-limite) sem nunca condicionar o mecanismo de autenticação usado — que permanece um único bilhete de rede nativo do domínio para qualquer categoria de máquina, eliminando a necessidade de qualquer segredo persistente gerido fora do domínio.
- O mecanismo de bootstrap inverte deliberadamente a direcção da ligação de rede para operações multi-passo — a máquina alvo passa a contactar o orquestrador, não o inverso — usando para tal a criação remota de processo apenas como acto único de arranque, e não como canal de execução contínuo.
- A detecção de "operação multi-passo" que despoleta este mecanismo é inferida automaticamente do padrão de pedidos recentes por máquina, não declarada explicitamente pelo autor do pedido.
- A governação de concorrência em quatro camadas simultâneas e independentes (global / credencial / máquina / operador), adquiridas em cascata com um único prazo de espera, é aplicada uniformemente a ambos os mecanismos de transporte (directo e por bootstrap).

---

## Mecanismo C — Compilação de instrução em linguagem natural para grafo de execução determinístico, com revisão automática prévia à persistência

### Campo técnico
Sistemas de automação de operações de tecnologias de informação que traduzem instruções expressas em linguagem natural por um operador humano numa sequência de acções executável, envolvendo múltiplos sistemas externos distintos.

### Problema técnico anterior
Ferramentas de automação de operações exigem tipicamente que o operador construa manualmente, através de uma interface de edição estruturada (formulários, blocos visuais ligados manualmente), a sequência de passos a executar — processo que exige conhecimento prévio da ferramenta de automação e tempo de configuração proporcional à complexidade da automação. Alternativas baseadas inteiramente em modelos de linguagem, que reinterpretam a instrução original em texto livre a cada execução, incorrem em custo computacional (invocação do modelo) e latência a cada execução periódica ou accionada por evento, e estão sujeitas a variação de comportamento entre execuções da mesma automação nominal, porque a interpretação da instrução não é fixada num momento de compilação distinto do momento de execução.

### Sumário do mecanismo
O sistema compila, num único momento de definição da automação, uma instrução em linguagem natural, escrita pelo operador sem sintaxe formal obrigatória, numa estrutura de grafo dirigido de nós e arestas, usando um vocabulário fechado e predefinido de tipos de nó que cobre chamada a sistemas externos já configurados, consulta a bases de dados, execução de comandos remotos (num único alvo ou em lista dinâmica de alvos), filtragem condicional sem custo computacional de modelo de linguagem, e um tipo de nó reservado para julgamento verdadeiramente ambíguo. Este grafo compilado é depois executado deterministicamente, sem nova invocação de modelo de linguagem em cada execução salvo nos nós do tipo de julgamento ambíguo. Antes de o grafo compilado ser persistido para uso futuro, o sistema submete-o a uma segunda passagem automática de revisão estática, distinta da compilação inicial, especificamente dirigida à detecção de um conjunto definido de erros comuns de geração.

### Descrição detalhada

1. **Compilação assistida por catálogo.** No momento de definição da automação, o sistema fornece ao processo de compilação um catálogo actualizado dos sistemas externos já configurados e das operações disponíveis em cada um, extraído da configuração viva do sistema, não de conhecimento genérico do modelo de linguagem. A instrução do operador pode referenciar esses sistemas por sintaxe explícita ou por menção em prosa livre reconhecida por comparação insensível a maiúsculas/minúsculas contra o catálogo fornecido — o sistema não exige sintaxe formal para reconhecer uma referência válida.

2. **Vocabulário fechado de tipos de nó.** A estrutura de grafo resultante da compilação está restrita a um conjunto fixo e predefinido de tipos de nó, cada um com uma forma de parametrização própria: um nó de origem (evento desencadeador), nós de chamada a operação de sistema externo já configurado (com possibilidade de repetição automática sobre cada elemento de uma lista obtida de um nó anterior), nós de consulta a base de dados configurada, nós de execução de comando remoto sobre um único alvo fixo ou sobre uma lista dinâmica de alvos obtida de um passo anterior, nós de filtragem condicional determinística sobre o resultado estruturado de um passo anterior, e um nó de decisão por modelo de linguagem, reservado para instrução do operador identificada como genuinamente ambígua ou dependente de julgamento textual, e não como substituto de qualquer outro tipo de nó cuja acção seja determinável a partir da instrução.

3. **Propagação de dados estruturados entre nós, não serializados.** O resultado de cada nó é mantido em estrutura de dados nativa (não convertida para texto) durante a propagação para nós subsequentes, permitindo que um nó a jusante aceda a um campo específico e aninhado do resultado de um nó anterior, itere sobre uma lista de resultados aplicando a mesma operação a cada elemento, ou filtre por um campo e um operador de comparação — sem que essa navegação estruturada dependa de nova interpretação por modelo de linguagem em tempo de execução.

4. **Referência estável ao nó de origem, independente do identificador atribuído.** Uma sintaxe de referência dedicada resolve sempre para o nó de origem do grafo, independentemente do identificador interno que lhe tenha sido atribuído na compilação ou em edição manual subsequente — eliminando a dependência de o autor da instrução, ou de uma edição manual posterior do grafo, conhecer ou preservar esse identificador interno.

5. **Revisão automática estática, distinta e posterior à compilação.** Antes de o grafo compilado no passo 2 ser persistido para execução futura, o sistema submete-o, juntamente com a instrução original, a uma segunda invocação do modelo de linguagem, com uma instrução de sistema distinta da usada na compilação, dirigida especificamente à detecção de um conjunto predefinido de padrões de erro de geração — nomeadamente valores literais fixos usados dentro de uma estrutura de repetição que deveria referenciar antes uma variável de iteração, verificação redundante de um recurso do sistema operativo dentro de uma repetição quando essa verificação deveria ocorrer uma única vez, e referências a nós inexistentes no grafo. Esta segunda passagem produz, quando aplicável, uma versão corrigida do grafo completo, sem execução real de qualquer script ou chamada durante a revisão.

6. **Execução determinística pós-compilação.** Uma vez persistido, o grafo é executado por ordenação topológica dos seus nós, sem nova invocação de modelo de linguagem para nós que não sejam do tipo de decisão por julgamento — o custo computacional do modelo de linguagem ocorre inteiramente no momento de compilação e revisão, não repetidamente a cada execução periódica ou accionada por evento subsequente da mesma automação.

7. **Reversibilidade texto ↔ grafo.** O sistema é capaz tanto de compilar uma instrução em linguagem natural num grafo (passos 1–5), como de gerar, a partir de um grafo editado directamente numa representação visual, uma descrição em linguagem natural coerente com a ordem e a semântica real dos nós presentes — mantendo as duas representações (texto e grafo) sincronizadas independentemente de qual delas foi editada por último.

### Elementos de novidade
- A compilação distingue explicitamente, por tipo de nó, entre acções deterministicamente identificáveis na instrução original e as genuinamente dependentes de julgamento textual — restringindo o nó de julgamento por modelo de linguagem a este último caso, em vez de usar julgamento por modelo de linguagem como mecanismo por omissão.
- A existência de uma segunda passagem de revisão automática, estruturalmente distinta da compilação inicial e dirigida a uma lista predefinida e concreta de erros de geração (não uma revisão genérica de qualidade), aplicada antes da persistência e nunca durante a execução.
- A propagação de resultados entre nós do grafo em estrutura de dados nativa, preservando a capacidade de navegação/filtragem/iteração por campo em tempo de execução determinística, sem depender de nova interpretação por modelo de linguagem para essa navegação.
- A separação temporal entre o custo computacional do modelo de linguagem (confinado ao momento de compilação e revisão) e a execução determinística repetida da mesma automação ao longo do tempo.

---

## Notas para os advogados / AOPI

- Cada mecanismo acima corresponde a um pedido (ou a um conjunto de reivindicações dentro de um mesmo pedido, a decidir convosco) independente dos outros dois — não há dependência técnica entre A, B e C que exija tratá-los como uma única invenção.
- Os diagramas de fluxo de cada mecanismo serão entregues como ficheiros separados, referenciados como Figura 1 (Mecanismo A), Figura 2 (Mecanismo B) e Figura 3 (Mecanismo C).
- A evidência de data de concepção (histórico de alterações ao código-fonte com data) será compilada e entregue em anexo separado, por mecanismo.
- Este documento não contém código-fonte literal deliberadamente — descreve o processo/método, não a implementação linha a linha, por ser essa a forma adequada a reivindicações de método técnico.
