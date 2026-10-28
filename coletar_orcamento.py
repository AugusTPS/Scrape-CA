"""Coleta a LOA 2025 de Cruz Alta e resume a dotação inicial por órgão.
BeautifulSoup localiza item-transparencia[dados]; o JSON em Base64 contém
os anexos. A paginação é documental: percorremos as páginas do PDF, não
inventamos um parâmetro GET. Consulte README.md para método e limitações.
"""
import argparse
import base64
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Configura os endereços, a pasta de trabalho e os padrões usados na extração.
URL = 'https://cruzalta.atende.net/transparencia/item/loa-lei-orcamentaria-anual'
BASE = 'https://cruzalta.atende.net/'
PASTA = Path(__file__).resolve().parent
SECAO = 'Anexo 02 - Despesa Seg. Cat. Econômica(Órg. Unid.)'
DINHEIRO = r'([\d.]+,\d{2})'


# Converte valores do formato brasileiro para números decimais.
def decimal_br(valor):
    return Decimal(valor.replace('.', '').replace(',', '.'))


# Formata valores monetários no padrão brasileiro para exibição.
def reais(valor):
    return f'{valor:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def baixar():
    """Lê a página pública, encontra anexos pelo nome e baixa apenas dois PDFs."""
    fontes = PASTA / 'fontes'
    fontes.mkdir(exist_ok=True)
    with requests.Session() as sessao:
        resposta = sessao.get(URL, timeout=60)
        resposta.raise_for_status()
        (fontes / 'pagina_loa.html').write_bytes(resposta.content)
        soup = BeautifulSoup(resposta.content, 'html.parser')
        tag = soup.select_one('item-transparencia[dados]')
        if tag is None:
            raise ValueError('Componente com anexos ausente; revise a estrutura do portal.')
        # Decodifica os dados em Base64 do portal para obter a lista de anexos.
        dados = json.loads(base64.b64decode(tag.get('dados', ''), validate=True))
        anexos = dados.get('anexo', [])
        escolhas = [('loa 2025 - parte 2.pdf', 'loa_2025_anexos.pdf'),
                    ('lei 3738 - loa 2025.pdf', 'lei_3738.pdf')]
        manifesto = {'pagina_alvo': URL, 'coletado_em_utc': datetime.now(timezone.utc).isoformat(), 'arquivos': []}
        # Baixa os PDFs escolhidos e registra suas fontes e hashes para conferência.
        for nome, destino in escolhas:
            encontrados = [a for a in anexos if a.get('nome', '').strip().casefold() == nome]
            if len(encontrados) != 1:
                raise ValueError(f'Esperado um anexo com nome {nome}; encontrados {len(encontrados)}.')
            anexo = encontrados[0]
            r = sessao.get(BASE + 'atende.php', params={
                'rot': '1', 'aca': '119', 'ajax': 't', 'processo': 'downloadFile',
                'file': anexo['id'], 'sistema': 'WPO', 'classe': 'UploadMidia'}, timeout=120)
            r.raise_for_status()
            if not r.content.startswith(b'%PDF'):
                raise ValueError('O servidor não retornou um PDF.')
            (fontes / destino).write_bytes(r.content)
            manifesto['arquivos'].append({'nome_portal': nome, 'arquivo_local': destino,
                'url': r.url, 'sha256': hashlib.sha256(r.content).hexdigest()})
    # Salva o histórico da coleta para permitir conferência e reprocessamento offline.
    (fontes / 'manifesto.json').write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifesto


def texto_pdf(caminho):
    """Extrai o texto do PDF preservando a disposição das tabelas."""
    executavel = (
        PASTA
        / 'Release-26.09.0-0'
        / 'poppler-26.09.0'
        / 'Library'
        / 'bin'
        / 'pdftotext.exe'
    )

    try:
        return subprocess.check_output(
            [
                str(executavel),
                '-layout',
                str(caminho),
                '-'
            ],
            encoding='utf-8'
        )
    except FileNotFoundError as erro:
        raise RuntimeError(
            'Poppler não encontrado. Confira a pasta de instalação.'
        ) from erro


def extrair(texto, manifesto):
    """Extrai somente Total Unidade: somar também subtotais duplicaria despesas."""
    registros, chaves = [], set()
    orgao = unidade = None
    url_pdf = next(a['url'] for a in manifesto['arquivos'] if a['arquivo_local'] == 'loa_2025_anexos.pdf')
    # Percorre a seção de despesas do PDF para identificar órgãos, unidades e seus totais.
    for pagina, conteudo in enumerate(texto.split('\f'), 1):
        if SECAO not in conteudo:
            continue
        for linha in conteudo.splitlines():
            linha = ' '.join(linha.split())
            m = re.match(r'Órgão:\s*(\d+)\s*-\s*(.+)', linha)
            if m:
                orgao = (m[1].zfill(2), m[2])
                continue
            m = re.match(r'Unidade:\s*(\d+)\s*-\s*(.+)', linha)
            if m:
                unidade = (m[1].zfill(3), m[2])
                continue
            m = re.fullmatch(r'Total Unidade\s+' + DINHEIRO, linha)
            if not m:
                continue
            # Valida a identificação e impede o registro de unidades duplicadas.
            if orgao is None or unidade is None:
                raise ValueError('Total encontrado sem órgão/unidade identificados.')
            chave = (orgao[0], unidade[0])
            if chave in chaves:
                raise ValueError(f'Unidade repetida: {chave}. Não somada automaticamente.')
            chaves.add(chave)
            # Guarda o orçamento da unidade com os dados que permitem rastrear sua origem.
            registros.append({'ano': 2025, 'codigo_orgao': orgao[0], 'orgao': orgao[1],
                'codigo_unidade': unidade[0], 'unidade': unidade[1],
                'dotacao_inicial_reais': str(decimal_br(m[1])),
                'escopo': 'Legislativo' if orgao[0] == '01' else 'Executivo e operações especiais',
                'pagina_pdf': pagina, 'url_fonte': url_pdf,
                'coletado_em_utc': manifesto['coletado_em_utc']})
            unidade = None
    return registros


# Confere os valores com a lei e gera os arquivos JSON e o relatório.
def gerar(registros, lei):
    # Separa o Executivo e verifica se a coleta tem a quantidade mínima esperada de unidades.
    prefeitura = [r for r in registros if r['codigo_orgao'] != '01']
    if len(prefeitura) < 30:
        raise ValueError(f'Coleta insuficiente: {len(prefeitura)} unidades do Executivo.')
    # Confere se a soma das unidades coincide com o total fixado no art. 2º da lei.
    total = sum((Decimal(r['dotacao_inicial_reais']) for r in registros), Decimal(0))
    match = re.search(r'Art\.\s*2[º°].*?R\$\s*' + DINHEIRO, lei, re.S)
    if match is None or total != decimal_br(match[1]):
        raise ValueError('Soma das unidades não confere com o total do art. 2º da lei.')
    # Agrupa os valores por órgão e calcula os percentuais do Executivo em ordem de orçamento.
    por_orgao = defaultdict(lambda: Decimal(0))
    nomes = {}
    for r in prefeitura:
        por_orgao[r['codigo_orgao']] += Decimal(r['dotacao_inicial_reais'])
        nomes[r['codigo_orgao']] = r['orgao']
    base = sum(por_orgao.values())
    resumo = [{'codigo_orgao': c, 'orgao': nomes[c], 'dotacao_inicial_reais': str(v),
               'percentual_do_executivo': str((v / base * 100).quantize(Decimal('0.01')))}
              for c,v in sorted(por_orgao.items(), key=lambda x: x[1], reverse=True)]
    # Exporta os dados detalhados, o resumo por órgão e a conferência municipal.
    for nome, objeto in [('orcamento_extraido.json', prefeitura), ('resumo_por_orgao.json', resumo),
                         ('conferencia_municipio.json', registros)]:
        (PASTA / nome).write_text(json.dumps(objeto, ensure_ascii=False, indent=2), encoding='utf-8')
    # Monta o relatório com os totais, a tabela por órgão e as orientações de interpretação.
    rel = ['# Cruz Alta — orçamento destinado por área em 2025', '',
        '**Escopo:** dotação inicial da LOA 2025 (Lei Municipal 3.738/24). Todas as fontes de receita. '
        'Não representa gasto pago, dotação atualizada nem aplicação exclusiva de impostos.', '',
        f'Foram extraídas **{len(prefeitura)} unidades do Executivo**, agrupadas em **{len(resumo)} órgãos**, '
        'mantendo os nomes oficiais. A Câmara Municipal foi separada.', '',
        f'- Executivo e operações especiais: **R$ {reais(base)}**.',
        f'- Câmara Municipal, fora do ranking: **R$ {reais(total-base)}**.',
        f'- Município consolidado: **R$ {reais(total)}**, conferido com o art. 2º da lei.', '',
        '| Código | Órgão / área oficial | Orçamento inicial (R$) | % do Executivo |',
        '|---|---|---:|---:|']
    for r in resumo:
        rel.append(f"| {r['codigo_orgao']} | {r['orgao']} | {reais(Decimal(r['dotacao_inicial_reais']))} | {r['percentual_do_executivo'].replace('.', ',')}% |")
    rel += ['', '## Como interpretar', '',
        'O setor jurídico aparece como **Procuradoria-Geral do Município**. Educação e Saúde seguem '
        'a classificação por órgão, que não é idêntica à classificação por função ou aos cálculos '
        'de mínimos constitucionais. Operações Especiais permanece em linha própria, sem redistribuição artificial.', '',
        'O orçamento inclui impostos, taxas, contribuições e transferências, entre outras receitas. '
        'Os totais acima não permitem atribuir exclusivamente impostos a cada órgão. '
        'Não se deve chamar recursos livres de “impostos” nem tratar transferências como arrecadação tributária própria.', '',
        'Trata-se de autorização inicial para 2025, não de comprovação de execução. Alterações orçamentárias, '
        'valores empenhados, liquidados e pagos não estão medidos neste relatório.', '',
        '## Fontes e rastreabilidade', '', URL, '',
        'Anexo 02 — Despesa Seg. Cat. Econômica (Órg. Unid.), páginas 10–16 do arquivo '
        '“loa 2025 - parte 2.pdf”, e “lei 3738 - loa 2025.pdf”. O anexo foi emitido em 14/11/2024 '
        'e integra o conjunto publicado pelo portal para 2025; o total coincide com a lei sancionada. '
        'Cada registro informa a página, URL e data da coleta. Os documentos baixados e seus hashes '
        'estão na pasta fontes.', '',
        '## Conferências', '',
        f'{len(registros)} unidades municipais únicas; {len(prefeitura)} sem a Câmara; '
        'soma exata em Decimal; ausência de duplicação entre totais de unidade e de órgão. '
        'Os percentuais usam como denominador apenas o Executivo e operações especiais e podem '
        'apresentar diferença de arredondamento na soma.']
    (PASTA/'RELATORIO.md').write_text('\n'.join(rel)+'\n',encoding='utf-8')
    print(f'OK: {len(prefeitura)} registros; {len(resumo)} órgãos; total Executivo R$ {reais(base)}.')


# Coordena a coleta ou leitura offline dos documentos e a geração dos resultados.
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', help='Reprocessa os PDFs e manifesto já baixados.')
    args = parser.parse_args()
    manifesto = (json.loads((PASTA/'fontes/manifesto.json').read_text(encoding='utf-8'))
                 if args.offline else baixar())
    gerar(extrair(texto_pdf(PASTA/'fontes/loa_2025_anexos.pdf'), manifesto),
          texto_pdf(PASTA/'fontes/lei_3738.pdf'))


# Inicia o processamento quando o arquivo é executado diretamente.
if __name__ == '__main__':
    main()
