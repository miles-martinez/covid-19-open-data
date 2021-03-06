# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path
from typing import Any, Dict, List

from pandas import DataFrame, concat

from lib.case_line import convert_cases_to_time_series
from lib.cast import numeric_code_as_string, safe_int_cast
from lib.pipeline import DataSource
from lib.time import datetime_isoformat, date_today
from lib.utils import aggregate_admin_level, table_rename
from pipelines.epidemiology.br_authority import _IBGE_STATES


_column_adapter = {
    # "document_id": "",
    # "paciente_id": "",
    "paciente_idade": "age",
    # "paciente_dataNascimento": "",
    "paciente_enumsexobiologico": "sex",
    # "paciente_racaCor_codigo": "",
    # "paciente_racaCor_valor": "",
    "paciente_endereco_coibgemunicipio": "subregion2_code",
    # "paciente_endereco_coPais": "",
    # "paciente_endereco_nmMunicipio": "",
    # "paciente_endereco_nmPais": "",
    "paciente_endereco_uf": "subregion1_code",
    # "paciente_endereco_cep": "",
    # "paciente_nacionalidade_enumNacionalidade": "",
    # "estabelecimento_valor": "",
    # "estabelecimento_razaoSocial": "",
    # "estalecimento_noFantasia": "",
    # "estabelecimento_municipio_codigo": "",
    # "estabelecimento_municipio_nome": "",
    # "estabelecimento_uf": "",
    # "vacina_grupoAtendimento_codigo": "",
    # "vacina_grupoAtendimento_nome": "",
    # "vacina_categoria_codigo": "",
    # "vacina_categoria_nome": "",
    # "vacina_lote": "",
    # "vacina_fabricante_nome": "",
    # "vacina_fabricante_referencia": "",
    "vacina_dataaplicacao": "date_new_vaccine_doses_administered",
    "vacina_descricao_dose": "_dose_information",
    # "vacina_codigo": "",
    "vacina_nome": "vaccine_manufacturer",
    # "sistema_origem": "",
}


def _process_partition(cases: DataFrame) -> DataFrame:
    cases = cases.copy()

    # Extract information about whether doses were first (partial immunization) or second (full)
    cases["date_new_persons_vaccinated"] = None
    cases["date_new_persons_fully_vaccinated"] = None
    first_dose_mask = cases["_dose_information"].str.strip().str.slice(0, 1) == "1"
    second_dose_mask = cases["_dose_information"].str.strip().str.slice(0, 1) == "2"
    cases.loc[first_dose_mask, "date_new_persons_vaccinated"] = cases.loc[
        first_dose_mask, "date_new_vaccine_doses_administered"
    ]
    cases.loc[second_dose_mask, "date_new_persons_fully_vaccinated"] = cases.loc[
        second_dose_mask, "date_new_vaccine_doses_administered"
    ]

    # Drop columns which we have no use for
    cases = cases[[col for col in cases.columns if not col.startswith("_")]]

    # Make sure our region codes are of type str
    cases["subregion2_code"] = cases["subregion2_code"].apply(
        lambda x: numeric_code_as_string(x, 6)
    )

    # Convert ages to int, and translate sex (no "other" sex/gender reported)
    cases["age"] = cases["age"].apply(safe_int_cast)
    cases["sex"] = cases["sex"].str.lower().apply({"m": "male", "f": "female"}.get)

    # Convert to time series format
    data = convert_cases_to_time_series(cases, index_columns=["subregion1_code", "subregion2_code"])

    # Convert date to ISO format
    data["date"] = data["date"].str.slice(0, 10)
    data["date"] = data["date"].apply(lambda x: datetime_isoformat(x, "%Y-%m-%d"))

    # Get rid of bogus records
    data = data.dropna(subset=["date"])
    data = data[data["date"] >= "2020-01-01"]
    data = data[data["date"] < date_today(offset=1)]

    # Aggregate data by country
    country = aggregate_admin_level(data, ["date", "age", "sex"], "country")
    country["key"] = "BR"

    # Aggregate data by state
    state = (
        data.drop(columns=["subregion2_code"])
        .groupby(["date", "subregion1_code", "age", "sex"])
        .sum()
        .reset_index()
    )
    state["key"] = "BR_" + state["subregion1_code"]

    # We can derive the key from subregion1 + subregion2
    data = data[data["subregion2_code"].notna() & (data["subregion2_code"] != "")]
    data["key"] = "BR_" + data["subregion1_code"] + "_" + data["subregion2_code"]

    return concat([country, state, data])


class BrazilDataSource(DataSource):
    def fetch(
        self,
        output_folder: Path,
        cache: Dict[str, str],
        fetch_opts: List[Dict[str, Any]],
        skip_existing: bool = False,
    ) -> Dict[str, str]:
        opts = dict(fetch_opts[0])
        url_tpl = opts.pop("url")

        # Each state has its own URL
        urls = []
        for state in _IBGE_STATES.values():
            # Keep the date in template format so we can substitute it later
            url = url_tpl.format(date="{date}", subregion1_code=state)
            urls.append(dict(name=state, url=url, **opts))

        # Pass the actual URLs down to fetch it
        return super().fetch(output_folder, cache, urls, skip_existing=skip_existing)

    def parse(self, sources: Dict[str, str], aux: Dict[str, DataFrame], **parse_opts) -> DataFrame:
        # Manipulate the parse options here because we have access to the columns adapter and we
        # can then limit the columns being read to save space.
        parse_opts = {
            **dict(parse_opts),
            "error_bad_lines": False,
            "usecols": _column_adapter.keys(),
        }
        return super().parse(sources, aux, **parse_opts)

    def parse_dataframes(
        self, dataframes: Dict[Any, DataFrame], aux: Dict[str, DataFrame], **parse_opts
    ) -> DataFrame:
        return _process_partition(table_rename(list(dataframes.values())[0], _column_adapter))
