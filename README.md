# OllamaPilot

Versão **0.0.2**. Agente local para Windows que conversa com um servidor [Ollama](https://ollama.com). A janela é do programa. O modelo lê e grava arquivos, executa programas instalados, age sobre as janelas deles e, quando você tem direito sobre o arquivo, analisa um executável com o Ghidra.

OllamaPilot não escolhe uma pasta sozinho. O caminho absoluto vai na conversa, por exemplo `E:\laboratorio`. O que altera o computador passa por um cartão de autorização antes de acontecer.

Licença: [GNU Affero General Public License v3](LICENSE). Copyright (C) 2026 Evandro Massini.

## Usar o executável

Na página de [releases](https://github.com/EvandroMassini/OllamaPilot/releases/tag/v0.0.2), baixe `OllamaPilot-0.0.2-windows-x64.zip`, extraia a pasta e abra `OllamaPilot.exe`.

Deixe `OllamaPilot.exe` e a pasta `_internal` no mesmo lugar. Separar os dois impede o programa de abrir. O pacote não traz conversas, chaves nem a pasta `storage`: ela é criada na primeira execução, ao lado do executável.

É preciso um Ollama acessível na rede, em geral em outra máquina. Para analisar executável, o Ghidra precisa estar instalado. A pasta configurada é a que contém `support\analyzeHeadless.bat`.

## Primeira conversa

1. Abra **Configurações** e informe a URL do Ollama. Use o botão de teste.
2. Escolha o modelo na barra de cima. Ao lado dele aparecem as capacidades que o Ollama informou: ferramentas, visão, raciocínio e contexto.
3. Escreva o pedido. Se o trabalho for em arquivos, inclua a pasta, ou use **Pasta de trabalho**.

Sem a capacidade de ferramentas, o modelo responde só com texto. Sem visão, a imagem não é enviada e a janela avisa.

## O que a janela faz

- Conversa com resposta em fluxo, raciocínio separado, métricas e interrupção.
- Modelos: instalar, ver detalhes, carregar, liberar, copiar, excluir e criar um modelo com instrução própria.
- Anexos de texto, código, PDF, DOCX, XLSX e imagens PNG, JPEG, GIF ou WebP.
- Biblioteca com embeddings. Se o modelo de embedding não estiver no servidor, a busca continua por texto.
- PostgreSQL em três modos: desligado, somente leitura, ou leitura e escrita. O banco das configurações é o padrão e pode ficar vazio. Outro banco pede confirmação. Escrita só existe no modo leitura e escrita, e o cartão mostra o SQL.
- Pesquisa web opcional, pela API do Ollama.
- PDF, DOCX, XLSX e ZIP no caminho dito na conversa, com título, seções, listas e tabelas. Sem caminho, o arquivo fica em Arquivos gerados por 24 horas. Texto, Markdown, CSV, JSON, XML, HTML e código também podem ser gerados.
- Exportação da conversa em Markdown.

## Autorização

Ações que alteram a máquina, executam um programa, mexem numa janela ou chamam o Ghidra aparecem num cartão com o alvo e o efeito. Gravar e editar mostram o diff.

- **Autorizar uma vez** vale só para aquela ação.
- **Autorizar neste chat** vale para o resto da conversa, para o tipo de ação descrito no cartão, e fica salva nela até **Nova conversa**.
- Recusar a mesma ação, com os mesmos argumentos, não mostra o cartão de novo naquele turno. Repetir sem progresso encerra o turno com uma explicação.

**Autorizar neste chat** para arquivos vale dentro da pasta de trabalho. O mesmo tipo de ação fora dela pede o cartão outra vez.

Antes de gravar, editar ou excluir, o OllamaPilot guarda a versão anterior em `storage\backups`: as últimas 100 alterações, até 200 MB. Peça para desfazer citando o caminho. A exclusão recursiva de uma pasta não pode ser desfeita, e o cartão avisa.

Um programa de linha de comando é aguardado até o tempo limite, e a resposta traz o código de saída. Um programa com janela é só iniciado. O agente segue por essa janela: teclado, clique no controle pelo nome e leitura dos textos visíveis.

Quando a mensagem cita dois ou mais caminhos absolutos, gravar espera um plano. O agente chama a ferramenta de plano e o cartão mostra a lista. Autorize para a gravação seguir.

## Conversas anteriores

Cada conversa é salva em `storage\conversas` depois de cada resposta. **Nova conversa** não apaga a anterior. Em **Conversas anteriores**:

- **Abrir** reabre a conversa. As autorizações "neste chat" não voltam.
- **Mudar o título** vale para a linha selecionada e sobrevive aos salvamentos seguintes.
- **Fixar** coloca a conversa no topo. As fixadas ficam em ordem alfabética. As outras continuam da mais recente para a mais antiga.
- **Excluir** pede confirmação. Pode ser por seleção ou por período. Não há como desfazer.

Os arquivos guardam o texto da conversa e dos anexos de texto. Não guardam as imagens.

Enquanto o modelo responde, ou enquanto um programa demora, um arco animado aparece na resposta e abaixo da caixa de texto.

## Contexto e modelos online

O histórico é ajustado ao contexto do modelo a cada consulta. O pedido atual permanece. As trocas antigas viram um resumo com os pedidos, as ferramentas, as respostas e os caminhos. A conversa completa continua na janela.

Em **Configurações**, **Modelos** decide a lista:

- **Somente modelos locais:** só os modelos do servidor Ollama configurado. O botão **Saldo** some.
- **Locais e online:** inclui os modelos de ollama.com e exige a chave de API.

Um modelo online só é usado quando você o escolhe. Com ele selecionado, o painel mostra **Nuvem** e avisa que a conversa e os anexos vão para ollama.com.

## Sala limpa

Para estudar um programa parecido com um existente, a mensagem precisa dizer que você tem direito de analisar aquele arquivo. Sem isso, a leitura do binário e o Ghidra não são chamados.

O caminho é: ler o formato do arquivo, rodar o Ghidra em modo de análise (o executável estudado não é aberto) e gravar uma especificação do comportamento observável. Um programa novo, se você pedir, sai dessa especificação. O pseudocódigo do Ghidra não é copiado para o projeto novo.

A primeira análise devolve bibliotecas e textos do programa, não o pseudocódigo de cada função. Para uma função, o pedido seguinte nomeia essa função.

## Servidores MCP

Servidores MCP locais, por stdio, vêm desligados.

1. Em **Configurações**, **Servidores…**, **Abrir mcp.json**. Se o arquivo não existir, surge um exemplo em `storage\mcp.json`.
2. Declare o servidor e mude `"ativo"` para `true`.
3. Marque **Servidores MCP locais** e salve. **Conectar agora** mostra as ferramentas descobertas.

`padrao` vale para todas as ferramentas do servidor. `ferramentas` ajusta uma a uma: `perguntar` mostra o cartão, `liberada` dispensa o cartão, `bloqueada` esconde a ferramenta. Com modelo online, as ferramentas MCP ficam de fora, salvo `"permitir_nuvem": true` naquele servidor.

Um servidor que falha ao iniciar não é tentado de novo a cada mensagem. O motivo aparece na janela Servidores MCP.

## Registros

`storage\logs\erros.log` guarda falhas, sem chaves, senhas e tokens reconhecíveis, e roda ao passar de 1 MB. `storage\logs\turnos.jsonl` guarda a trilha de cada turno: modelo, ferramentas, autorização, duração e desfecho. Não guarda o texto da conversa. Dá para desligar em **Configurações**.

As chaves e a senha do PostgreSQL ficam no cofre do Windows, não no `settings.json` que vai para o disco.

## Rodar a partir do código

Windows e Python 3.10 ou superior.

```powershell
py -3 -m pip install -r requirements.txt
py -3 -m ollamapilot
```

`OllamaPilot.bat` faz o mesmo. Um turno sem a janela:

```powershell
py -3 -m ollamapilot --mensagem "..." --pasta E:\laboratorio --modelo gemma4:12b
```

`--aprovar workspace` concede de antemão só o que caberia dentro dessa pasta.

Para gerar o executável de novo:

```powershell
py -3 -m PyInstaller --noconfirm --distpath release --workpath build\pyinstaller OllamaPilot.spec
```

O resultado fica em `release\OllamaPilot`. A pasta `storage` dessa instalação não entra no Git. Se for recompilar por cima de uma instalação que você usa, copie `storage` antes: o empacotador apaga a pasta de saída.

## Testes

```powershell
py -3 -m unittest tests.test_core tests.test_robustez tests.test_mcp tests.test_historico tests.test_fases tests.test_platform tests.test_versao
```

Os testes usam um modelo simulado e pastas temporárias. Não chamam o Ollama, a rede, o banco nem programas, e não abrem a janela.

## Licença

Este programa é software livre sob a GNU Affero General Public License v3. Você pode usar, estudar, modificar e redistribuir. Se distribuir o programa ou uma versão modificada, inclusive como serviço pela rede, o código-fonte correspondente dessa versão também sai sob a AGPL. O texto completo está em [LICENSE](LICENSE).
