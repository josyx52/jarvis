# Diagramas de Fluxo — Base para Pedido de Patente

Diagramas de suporte à Memória Descritiva Técnica (`Jarvis-Memoria-Descritiva-Patente.md`). Cada figura usa numeração de referência própria, independente das outras — convenção normal em desenhos de patente, onde cada figura é lida isoladamente. Estes diagramas são um rascunho técnico funcional; a formatação final (traço a preto e branco, legendas numeradas conforme o padrão exigido pelo IAPI) fica a cargo de quem prepara a submissão.

---

## Figura 1 — Mecanismo A: Aprovação aplicada no despacho

```mermaid
flowchart TD
    N10["10 — Modelo de linguagem\nemite chamada de ferramenta"]
    N12{"12 — Ferramenta pertence\nao conjunto classificado\ncomo sujeita a aprovação?"}
    N14["14 — Despacho directo\npara execução"]
    N16{"16 — Ferramenta é da\nsubcategoria de conhecimento\npersistente de infra?"}
    N18{"18 — Canal de origem\ndo pedido"}
    N20["20 — Bloqueio síncrono:\ndevolve instrução para\nconfirmação via UI dedicada"]
    N22["22 — Estado pendente gravado\ncom token único + expiração\n(canal de mensagens)"]
    N24["24 — Automação: acção\npré-autorizada na criação\n→ despacho directo"]
    N26["26 — Estado pendente gravado\nincondicionalmente\n(mesmo em automação)"]
    N28["28 — Aguarda mensagem\nsubsequente no mesmo canal"]
    N30{"30 — Mensagem contém\ntermo de confirmação\nreconhecido?"}
    N32["32 — Resolução por token único\n(uso único, validado)"]
    N34["34 — Execução real /\nescrita definitiva"]
    N36["36 — Token expira\nsem resolução"]
    N38["38 — Acção descartada,\nnada persiste"]

    N10 --> N12
    N12 -- não --> N14
    N12 -- sim --> N16
    N16 -- sim --> N26
    N16 -- não --> N18
    N18 -- "interface conversacional directa" --> N20
    N18 -- "canal de mensagens" --> N22
    N18 -- "automação pré-autorizada" --> N24
    N22 --> N28
    N26 --> N28
    N28 --> N30
    N30 -- sim --> N32
    N30 -- não --> N36
    N32 --> N34
    N36 --> N38
```

**Legenda de referência:**
`10` chamada de ferramenta pelo modelo · `12` verificação de classificação estática · `14` caminho sem aprovação · `16` verificação de subcategoria incondicional · `18` determinação de canal · `20/22/24` os três comportamentos por canal · `26` desvio incondicional para pendência · `28-30` espera e reconhecimento de confirmação · `32` validação por token de uso único · `34` efeito persistente real · `36-38` expiração sem resolução.

---

## Figura 2 — Mecanismo B: Execução remota governada com autenticação dual e bootstrap por polling de saída

```mermaid
flowchart TD
    N40["40 — Pedido de execução\nremota recebido"]
    N42["42 — Consulta ao directório\ndo domínio (OU, SO)"]
    N44{"44 — Categoria\nda máquina"}
    N46["46 — Autenticação Kerberos\nvia conta de serviço gerida\n(sem password manual)"]
    N48["48 — Autenticação NTLM\ncom credenciais explícitas"]
    N50["50 — Governador de concorrência:\naquisição em cascata de\n4 contadores independentes"]
    N52["52 — Global"]
    N54["54 — Por credencial"]
    N56["56 — Por máquina"]
    N58["58 — Por operador"]
    N60{"60 — Pedidos recentes à\nmesma máquina excedem\nlimiar numa janela curta?"}
    N62["62 — Classificação:\noperação multi-passo"]
    N64["64 — Criação remota de\nprocesso (acto único) —\nlança processo temporário\nna máquina alvo"]
    N66["66 — Processo temporário inicia\npolling HTTP de saída\n(TTL definido)"]
    N68["68 — Máquina alvo contacta\norquestrador para obter\npróximo passo"]
    N70["70 — Orquestrador devolve\ncomando; máquina alvo\nexecuta e devolve resultado"]
    N72["72 — Execução directa síncrona\n(gestão remota tradicional)"]
    N74["74 — Verificação por ficheiro\nde resultado temporário +\nconsulta periódica"]
    N76["76 — Libertação dos 4\ncontadores, ordem inversa"]
    N78{"78 — Execuções sucessivas\nna mesma máquina dentro\nda janela de inactividade?"}
    N80["80 — Agrupadas numa\nsessão lógica de investigação"]
    N82["82 — Erro de acesso\nna categoria assumida?"]
    N84["84 — Correcção autónoma:\ntenta categoria alternativa"]

    N40 --> N42 --> N44
    N44 -- "posto de trabalho" --> N46
    N44 -- "servidor" --> N48
    N46 --> N50
    N48 --> N50
    N50 --> N52 --> N54 --> N56 --> N58
    N58 --> N60
    N60 -- sim --> N62 --> N64 --> N66 --> N68 --> N70
    N60 -- não --> N72 --> N74
    N70 --> N76
    N74 --> N76
    N76 --> N78
    N78 -- sim --> N80
    N46 -.-> N82
    N48 -.-> N82
    N82 -- sim --> N84 -.-> N44
```

**Legenda de referência:**
`40-44` recepção e classificação da máquina · `46/48` bifurcação de autenticação · `50-58` governador de concorrência em 4 camadas · `60-62` detecção automática de operação multi-passo · `64-70` transporte por bootstrap com polling de saída · `72-74` transporte directo com verificação por ficheiro · `76` libertação dos contadores · `78-80` agrupamento em sessão por janela de inactividade · `82-84` correcção autónoma de categoria.

---

## Figura 3 — Mecanismo C: Compilação de instrução em linguagem natural para grafo de execução determinístico

```mermaid
flowchart TD
    N90["90 — Instrução em linguagem\nnatural, escrita pelo operador"]
    N92["92 — Catálogo de sistemas\nexternos já configurados\n(injectado no processo\nde compilação)"]
    N94["94 — Compilação inicial:\ninstrução + catálogo\n→ modelo de linguagem"]
    N96["96 — Grafo estruturado\n(nós + arestas), vocabulário\nfechado de tipos de nó"]
    N98["98 — Revisão automática\nestática (2ª invocação,\ninstrução de sistema distinta)"]
    N100{"100 — Padrões de erro\nde geração detectados?"}
    N102["102 — Valor literal fixo\ndentro de repetição que\ndevia usar variável"]
    N104["104 — Verificação redundante\nde recurso de SO\ndentro de repetição"]
    N106["106 — Referência a\nnó inexistente"]
    N108["108 — Grafo corrigido"]
    N110["110 — Grafo persistido\n(sem execução real\ndurante a revisão)"]
    N112["112 — Execução futura:\nordenação topológica\ndos nós"]
    N114["114 — Nó de chamada a\nsistema externo /\nconsulta a BD / execução\nremota / filtro condicional"]
    N116["116 — Nó de decisão por\njulgamento (modelo de\nlinguagem), só quando\ngenuinamente ambíguo"]
    N118["118 — Propagação de\nresultado em estrutura\nde dados nativa\n(não serializada)"]
    N120["120 — Referência estável\nao nó de origem,\nindependente do id atribuído"]
    N122["122 — Edição manual\ndo grafo (representação\nvisual)"]
    N124["124 — Regeneração da\ndescrição em linguagem\nnatural a partir do grafo"]

    N90 --> N94
    N92 --> N94
    N94 --> N96
    N96 --> N98
    N98 --> N100
    N100 -- "102/104/106" --> N102
    N100 -- "102/104/106" --> N104
    N100 -- "102/104/106" --> N106
    N102 --> N108
    N104 --> N108
    N106 --> N108
    N100 -- "nenhum" --> N110
    N108 --> N110
    N110 --> N112
    N112 --> N114
    N112 --> N116
    N114 --> N118
    N116 --> N118
    N118 --> N120
    N110 -.-> N122
    N122 -.-> N124
    N124 -.-> N90
```

**Legenda de referência:**
`90-94` instrução original e compilação assistida por catálogo · `96` grafo resultante, vocabulário fechado · `98-108` segunda passagem de revisão estática e correcção · `110` persistência só após revisão · `112-116` execução determinística pós-compilação, com nó de julgamento isolado · `118` propagação estruturada entre nós · `120` referência estável ao nó de origem · `122-124` ciclo de reversibilidade texto↔grafo.

---

## Nota

As referências numéricas (10, 12, 14…) seguem a convenção comum de desenhos de patente — números pares, com espaço para inserção de elementos adicionais em revisões futuras sem renumerar tudo. Ajustar conforme a convenção específica exigida pelo AOPI.
