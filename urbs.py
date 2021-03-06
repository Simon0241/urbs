"""urbs: A linear optimisation model for distributed energy systems

urbs minimises total cost for providing energy in form of desired commodities
(usually electricity) to satisfy a given demand in form of timeseries. The
model contains commodities (electricity, fossil fuels, renewable energy
sources, greenhouse gases), processes that convert one commodity to another
(while emitting greenhouse gases as a secondary output), transmission for
transporting commodities between sites and storage for saving/retrieving
commodities.

"""
import coopr.pyomo as pyomo
import pandas as pd
from datetime import datetime
from operator import itemgetter
from random import random

COLORS = {
    'Biomass': (0, 122, 55),
    'Coal': (100, 100, 100),
    'Demand': (25, 25, 25),
    'Diesel': (116, 66, 65),
    'Gas': (237, 227, 0),
    'Elec': (0, 101, 189),
    'Heat': (230, 112, 36),
    'Hydro': (198, 188, 240),
    'Import': (128, 128, 200),
    'Lignite': (116, 66, 65),
    'Oil': (116, 66, 65),
    'Overproduction': (190, 0, 99),
    'Slack': (163, 74, 130),
    'Solar': (243, 174, 0),
    'Storage': (60, 36, 154),
    'Wind': (122, 179, 225),
    'Stock': (222, 222, 222),
    'Decoration': (128, 128, 128),
    'Grid': (128, 128, 128)}


def read_excel(filename):
    """Read Excel input file and prepare URBS input dict.

    Reads an Excel spreadsheet that adheres to the structure shown in
    data-example.xlsx. Two preprocessing steps happen here:
    1. Column titles in 'Demand' and 'SupIm' are split, so that
    'Site.Commodity' becomes the MultiIndex column ('Site', 'Commodity').
    2. The attribute 'annuity-factor' is derived here from the columns 'wacc'
    and 'depreciation' for 'Process', 'Transmission' and 'Storage'.

    Args:
        filename: filename to an Excel spreadsheet with the required sheets
            'Commodity', 'Process', 'Transmission', 'Storage', 'Demand' and
            'SupIm'.

    Returns:
        a dict of 6 DataFrames
        
    Example:
        >>> data = read_excel('data-example.xlsx')
        >>> data['commodity'].loc[('Global', 'CO2', 'Env'), 'max']
        150000000.0
    """
    with pd.ExcelFile(filename) as xls:
        commodity = xls.parse(
            'Commodity',
            index_col=['Sit', 'Com', 'Type'])
        process = xls.parse(
            'Process',
            index_col=['Sit', 'Pro', 'CoIn', 'CoOut'])
        transmission = xls.parse(
            'Transmission',
            index_col=['SitIn', 'SitOut', 'Tra', 'Com'])
        storage = xls.parse(
            'Storage',
            index_col=['Sit', 'Sto', 'Com'])
        demand = xls.parse(
            'Demand',
            index_col=['t'])
        supim = xls.parse(
            'SupIm',
            index_col=['t'])

    # prepare input data
    # split columns by dots '.', so that 'DE.Elec' becomes the two-level
    # column index ('DE', 'Elec')
    demand.columns = split_columns(demand.columns, '.')
    supim.columns = split_columns(supim.columns, '.')

    # derive annuity factor from WACC and depreciation periods
    process['annuity-factor'] = annuity_factor(
        process['depreciation'], process['wacc'])
    transmission['annuity-factor'] = annuity_factor(
        transmission['depreciation'], transmission['wacc'])
    storage['annuity-factor'] = annuity_factor(
        storage['depreciation'], storage['wacc'])

    data = {
        'commodity': commodity,
        'process': process,
        'transmission': transmission,
        'storage': storage,
        'demand': demand,
        'supim': supim}

    # sort nested indexes to make direct assignments work, cf
    # http://pandas.pydata.org/pandas-docs/stable/indexing.html#the-need-for-sortedness-with-multiindex
    for key in data:
        if isinstance(data[key].index, pd.core.index.MultiIndex):
            data[key].sortlevel(inplace=True)
    return data


def create_model(data, timesteps, dt=1):
    """Create a pyomo ConcreteModel URBS object from given input data.
    
    Args:
        data: a dict of 6 DataFrames with the keys 'commodity', 'process',
            'transmission', 'storage', 'demand' and 'supim'.
        timesteps: list of timesteps
        dt: timestep duration in hours (default: 1)
        
    Returns:
        a pyomo ConcreteModel object
    """
    m = pyomo.ConcreteModel()
    m.name = 'URBS'
    m.settings = {
        'dateformat': '%Y%m%dT%H%M%S',
        'timesteps': timesteps,
        }
    m.created = datetime.now().strftime(m.settings['dateformat'])

    # Preparations
    # ============
    # Data import. Syntax to access a value within equation definitions looks
    # like this:
    #
    #     m.process.loc[sit, pro, coin, cout][attribute]
    #
    get_inputs = itemgetter(
        "commodity", "process", "transmission", "storage",
        "demand", "supim")
    (m.commodity, m.process, m.transmission, m.storage,
        m.demand, m.supim) = get_inputs(data)

    # Sets
    # ====
    # Syntax: m.{name} = Set({domain}, initialize={values})
    # where name: set name
    #       domain: set domain for tuple sets, a cartesian set product
    #       values: set values, a list or array of element tuples
    
    # generate ordered timesteps
    m.t = pyomo.Set(
        initialize=m.settings['timesteps'],
        ordered=True,
        doc='Set of timesteps')
    m.tm = pyomo.Set(
        within=m.t,
        initialize=m.settings['timesteps'][1:],
        ordered=True,
        doc='Set of modelled timesteps')
    
    # site (e.g. north, middle, south...)
    m.sit = pyomo.Set(
        initialize=m.commodity.index.get_level_values('Sit').unique(),
        doc='Set of sites')
    
    # commodity (e.g. solar, wind, coal...)
    m.com = pyomo.Set(
        initialize=m.commodity.index.get_level_values('Com').unique(),
        doc='Set of commodities')
    
    # commodity type (e.g. SupIm, Demand, Stock, Env)
    m.com_type = pyomo.Set(
        initialize=m.commodity.index.get_level_values('Type').unique(),
        doc='Set of commodity types')
    
    # process (e.g. turb, wt, gt...)    
    m.pro = pyomo.Set(
        initialize=m.process.index.get_level_values('Pro').unique(),
        doc='Set of conversion processes')
    
    # tranmission (e.g. hvac)
    m.tra = pyomo.Set(
        initialize=m.transmission.index.get_level_values('Tra').unique(),
        doc='Set of tranmission technologies')
    
    # storage (e.g. hydrogen, pump storage)
    m.sto = pyomo.Set(
        initialize=m.storage.index.get_level_values('Sto').unique(),
        doc='Set of storage technologies')
    
    # cost_type
    m.cost_type = pyomo.Set(
        initialize=['Inv', 'Fix', 'Var', 'Fuel'],
        doc='Set of cost types (hard-coded)')

    # sets of existing tuples:
    # com_tuples = [('DE', 'Coal', 'Stock'), ('MA', 'Wind', 'SupIm'), ...]
    # pro_tuples = [('DE', 'pp', 'Coal', 'Elec'), ('NO', 'wt', 'Wind', 'Elec')]
    # sto_tuples = [('DE', 'bat', 'Elec'), ('NO', 'pst', 'Elec')...]
    m.com_tuples = pyomo.Set(within=m.sit*m.com*m.com_type,
                             initialize=m.commodity.index)
    m.pro_tuples = pyomo.Set(within=m.sit*m.pro*m.com*m.com,
                             initialize=m.process.index)
    m.tra_tuples = pyomo.Set(within=m.sit*m.sit*m.tra*m.com,
                             initialize=m.transmission.index)
    m.sto_tuples = pyomo.Set(within=m.sit*m.sto*m.com,
                             initialize=m.storage.index)

    # subsets of commodities by type
    # for equations that apply to only one commodity type
    m.com_supim = pyomo.Set(
        within=m.com,
        initialize=set(c[1] for c in m.com_tuples if c[2] == 'SupIm'))
    m.com_stock = pyomo.Set(
        within=m.com,
        initialize=set(c[1] for c in m.com_tuples if c[2] == 'Stock'))
    m.com_demand = pyomo.Set(
        within=m.com,
        initialize=set(c[1] for c in m.com_tuples if c[2] == 'Demand'))

    # Parameters
    # ==========
    # for model entities (commodity, process, transmission, storage) no
    # parames are needed, just use the DataFrames m.commodity, m.process,
    # m.storage and m.transmission directly.
    # Syntax: m.{name} = Param({domain}, initialize={values})
    # where name: param name
    #       domain: one or multiple model sets; empty for scalar parameters
    #       values: dict of parameter values, addressed by elements of domains
    # if domain is skipped, defines a scalar parameter with a single value
 
    # extrapolation to the yearly total
    # len(list) delivers the number of objects in list as integer    
    # meaning of product len(m.t) * dt ???
    # m.weight >= 1
    
    m.weight = pyomo.Param(initialize=float(8760) / (len(m.t) * dt))
    m.dt = pyomo.Param(initialize=dt)

    # Variables
    # =========
    # listed alphabetically
    # Syntax: m.{name} = Var({domain}, within={range}, doc={docstring})
    # where name: variable name
    #       domain: variable domain, consisting of one or multiple sets
    #       range: variable values, like Binary, Integer, NonNegativeReals
    #       docstring: a documentation string/short description
    
    # capacities
    m.cap_pro = pyomo.Var(
        m.pro_tuples,
        within=pyomo.NonNegativeReals,
        doc='Total process capacity (MW)')
    m.cap_pro_new = pyomo.Var(
        m.pro_tuples,
        within=pyomo.NonNegativeReals,
        doc='New process capacity (MW)')
    m.cap_tra = pyomo.Var(
        m.tra_tuples,
        within=pyomo.NonNegativeReals,
        doc='Total transmission capacity (MW)')
    m.cap_tra_new = pyomo.Var(
        m.tra_tuples,
        within=pyomo.NonNegativeReals,
        doc='New transmission capacity (MW)')
    m.cap_sto_c = pyomo.Var(
        m.sto_tuples,
        within=pyomo.NonNegativeReals,
        doc='Total storage size (MWh)')
    m.cap_sto_c_new = pyomo.Var(
        m.sto_tuples,
        within=pyomo.NonNegativeReals,
        doc='New storage size (MWh)')
    m.cap_sto_p = pyomo.Var(
        m.sto_tuples,
        within=pyomo.NonNegativeReals,
        doc='Total storage power (MW)')
    m.cap_sto_p_new = pyomo.Var(
        m.sto_tuples,
        within=pyomo.NonNegativeReals,
        doc='New  storage power (MW)')
    
    # emissions
    m.co2_pro_out = pyomo.Var(
        m.tm, m.pro_tuples,
        within=pyomo.NonNegativeReals,
        doc='CO2 emissions from process (t) per timestep')
    
    # costs
    m.costs = pyomo.Var(
        m.cost_type,
        within=pyomo.NonNegativeReals,
        doc='Costs by type (EUR/a)')
    
    # timeseries
    m.e_co_stock = pyomo.Var(
        m.tm, m.com_tuples,
        within=pyomo.NonNegativeReals,
        doc='Use of stock commodity source (MW) per timestep')
    m.e_pro_in = pyomo.Var(
        m.tm, m.pro_tuples,
        within=pyomo.NonNegativeReals,
        doc='Power flow into process (MW) per timestep')
    m.e_pro_out = pyomo.Var(
        m.tm, m.pro_tuples,
        within=pyomo.NonNegativeReals,
        doc='Power flow out of process (MW) per timestep')
    m.e_tra_in = pyomo.Var(
        m.tm, m.tra_tuples,
        within=pyomo.NonNegativeReals,
        doc='Power flow into transmission line (MW) per timestep')
    m.e_tra_out = pyomo.Var(
        m.tm, m.tra_tuples,
        within=pyomo.NonNegativeReals,
        doc='Power flow out of transmission line (MW) per timestep')
    m.e_sto_in = pyomo.Var(
        m.tm, m.sto_tuples,
        within=pyomo.NonNegativeReals,
        doc='Power flow into storage (MW) per timestep')
    m.e_sto_out = pyomo.Var(
        m.tm, m.sto_tuples,
        within=pyomo.NonNegativeReals,
        doc='Power flow out of storage (MW) per timestep')
    m.e_sto_con = pyomo.Var(
        m.t, m.sto_tuples,
        within=pyomo.NonNegativeReals,
        doc='Energy content of storage (MWh) in timestep')

    # Equation definition
    # ===================
    # listed by topic. All equations except the Objective function are
    # of type Constraint, although there are two semantics for those,
    # indicated by the name prefix (def, res).
    #  - def: definition, usually equations, defining variable values
    #  - res: restriction, usually inequalities, limiting variable values
    # topics
    #  - commodity
    #  - process
    #  - transmission
    #  - storage
    #  - emissions
    #  - costs

    # commodity
    
    # supply >= demand
    def res_demand_rule(m, tm, sit, com, com_type):
        if com not in m.com_demand:
            return pyomo.Constraint.Skip
        else:
            provided_power = - commodity_balance(m, tm, sit, com)
            return (provided_power >=
                    m.demand.loc[tm][sit, com])

    # calculation of import/purchase???                
    def def_e_co_stock_rule(m, tm, sit, com, com_type):
        if com not in m.com_stock:
            return pyomo.Constraint.Skip
        else:
            return (m.e_co_stock[tm, sit, com, com_type] ==
                    commodity_balance(m, tm, sit, com))
    
    # restriction for hourly stock purchase
    def res_stock_step_rule(m, tm, sit, com, com_type):
        if com not in m.com_stock:
            return pyomo.Constraint.Skip
        else:
            return (m.e_co_stock[tm, sit, com, com_type] <=
                    m.commodity.loc[sit, com, com_type]['maxperstep'])

    # calculate total consumption of commodity com
    def res_stock_total_rule(m, sit, com, com_type):
        if com not in m.com_stock:
            return pyomo.Constraint.Skip
        else:
            total_consumption = 0
            for tm in m.tm:
                total_consumption += (
                    m.e_co_stock[tm, sit, com, com_type] * m.dt)
            total_consumption *= m.weight
            return (total_consumption <=
                    m.commodity.loc[sit, com, com_type]['max'])

    # process
    
    # process capacity == new capacity + existing capacity
    def def_process_capacity_rule(m, sit, pro, coin, cout):
        return (m.cap_pro[sit, pro, coin, cout] ==
                m.cap_pro_new[sit, pro, coin, cout] +
                m.process.loc[sit, pro, coin, cout]['inst-cap'])
    
    # output == input * efficiency
    def def_process_output_rule(m, tm, sit, pro, coin, cout):
        return (m.e_pro_out[tm, sit, pro, coin, cout] ==
                m.e_pro_in[tm, sit, pro, coin, cout] *
                m.process.loc[sit, pro, coin, cout]['eff'])

    # output == installed capacity * supply per hour by intermittent sources            
    def def_intermittent_supply_rule(m, tm, sit, pro, coin, cout):
        if coin in m.com_supim:
            return (m.e_pro_in[tm, sit, pro, coin, cout] ==
                    m.cap_pro[sit, pro, coin, cout] *
                    m.supim.loc[tm][sit, coin])
        else:
            return pyomo.Constraint.Skip

    # calculation of co2 emissions per process
    # per timestep or per year???        
    def def_co2_emissions_rule(m, tm, sit, pro, coin, cout):
        return (m.co2_pro_out[tm, sit, pro, coin, cout] ==
                m.e_pro_in[tm, sit, pro, coin, cout] *
                m.process.loc[sit, pro, coin, cout]['co2'] *
                m.dt)
    
    # output <= capacity 
    def res_process_output_by_capacity_rule(m, tm, sit, pro, coin, cout):
        return (m.e_pro_out[tm, sit, pro, coin, cout] <=
                m.cap_pro[sit, pro, coin, cout])
    
    # lower bound <= process capacity <= upper bound
    def res_process_capacity_rule(m, sit, pro, coin, cout):
        return (m.process.loc[sit, pro, coin, cout]['cap-lo'],
                m.cap_pro[sit, pro, coin, cout],
                m.process.loc[sit, pro, coin, cout]['cap-up'])

    # transmission
    
    # transmission capacity == new capacity + existing capacity
    def def_transmission_capacity_rule(m, sin, sout, tra, com):
        return (m.cap_tra[sin, sout, tra, com] ==
                m.cap_tra_new[sin, sout, tra, com] +
                m.transmission.loc[sin, sout, tra, com]['inst-cap'])

    # output == input * efficiency
    def def_transmission_output_rule(m, tm, sin, sout, tra, com):
        return (m.e_tra_out[tm, sin, sout, tra, com] ==
                m.e_tra_in[tm, sin, sout, tra, com] *
                m.transmission.loc[sin, sout, tra, com]['eff'])
    
    # input <= transmission capacity
    def res_transmission_input_by_capacity_rule(m, tm, sin, sout, tra, com):
        return (m.e_tra_in[tm, sin, sout, tra, com] <=
                m.cap_tra[sin, sout, tra, com])
    
    # lower bound <= transmission capacity <= upper bound
    def res_transmission_capacity_rule(m, sin, sout, tra, com):
        return (m.transmission.loc[sin, sout, tra, com]['cap-lo'],
                m.cap_tra[sin, sout, tra, com],
                m.transmission.loc[sin, sout, tra, com]['cap-up'])
    
    # input capacity == output capacity
    def res_transmission_symmetry_rule(m, sin, sout, tra, com):
        return m.cap_tra[sin, sout, tra, com] == m.cap_tra[sout, sin, tra, com]

    # storage
    
    # content in timestep [t]
    # energy balance between timesteps [t-1] and [t]
    def def_storage_state_rule(m, t, sit, sto, com):
        return (m.e_sto_con[t, sit, sto, com] ==
                m.e_sto_con[t-1, sit, sto, com] +
                m.e_sto_in[t, sit, sto, com] *
                m.storage.loc[sit, sto, com]['eff-in'] * m.dt -
                m.e_sto_out[t, sit, sto, com] /
                m.storage.loc[sit, sto, com]['eff-out'] * m.dt)

    # storage power == new power + existing power            
    def def_storage_power_rule(m, sit, sto, com):
        return (m.cap_sto_p[sit, sto, com] ==
                m.cap_sto_p_new[sit, sto, com] +
                m.storage.loc[sit, sto, com]['inst-cap-p'])
    
    # storage capacity == new capacity + existing capacity
    def def_storage_capacity_rule(m, sit, sto, com):
        return (m.cap_sto_c[sit, sto, com] ==
                m.cap_sto_c_new[sit, sto, com] +
                m.storage.loc[sit, sto, com]['inst-cap-c'])
    
    # input <= power
    def res_storage_input_by_power_rule(m, t, sit, sto, com):
        return m.e_sto_in[t, sit, sto, com] <= m.cap_sto_p[sit, sto, com]
    
    # output <= power
    def res_storage_output_by_power_rule(m, t, sit, sto, co):
        return m.e_sto_out[t, sit, sto, co] <= m.cap_sto_p[sit, sto, co]

    # content <= capacity    
    def res_storage_state_by_capacity_rule(m, t, sit, sto, com):
        return m.e_sto_con[t, sit, sto, com] <= m.cap_sto_c[sit, sto, com]

    # lower bound <= power <= upper bound    
    def res_storage_power_rule(m, sit, sto, com):
        return (m.storage.loc[sit, sto, com]['cap-lo-p'],
                m.cap_sto_p[sit, sto, com],
                m.storage.loc[sit, sto, com]['cap-up-p'])

    # lower bound <= capacity <= upper bound
    def res_storage_capacity_rule(m, sit, sto, com):
        return (m.storage.loc[sit, sto, com]['cap-lo-c'],
                m.cap_sto_c[sit, sto, com],
                m.storage.loc[sit, sto, com]['cap-up-c'])

    # initialization of storage content in first timestep t[1]
    # initialization of storage content in final timestep t[len(m.t)]
    # content[t=1] == content[t=T] == capacity * factor
    def res_initial_and_final_storage_state_rule(m, t, sit, sto, com):
        if t == m.t[1]:  # first timestep (Pyomo uses 1-based indexing)
            return (m.e_sto_con[t, sit, sto, com] ==
                    m.cap_sto_c[sit, sto, com] *
                    m.storage.loc[sit, sto, com]['init'])
        elif t == m.t[len(m.t)]:  # last timestep
            return (m.e_sto_con[t, sit, sto, com] >=
                    m.cap_sto_c[sit, sto, com] *
                    m.storage.loc[sit, sto, com]['init'])
        else:
            return pyomo.Constraint.Skip

    # emissions
    
    # total co2 emissions <= maximum emissions
    def res_co2_emission_rule(m):
        return (pyomo.summation(m.co2_pro_out) * m.weight <=
                m.commodity.loc['Global', 'CO2', 'Env']['max'])

    # costs
    def def_costs_rule(m, cost_type):
        """Calculate total costs by cost type.

        Sums up process activity and capacity expansions
        and sums them in the cost types that are specified in the set
        m.cost_type. To change or add cost types, add/change entries
        there and modify the if/elif cases in this function accordingly.

        Cost types are
          - Investment costs for process power, storage power and
            storage capacity. They are multiplied by the annuity
            factors.
          - Fixed costs for process power, storage power and storage
            capacity.
          - Variables costs for usage of processes, storage and transmission.
          
        """
        if cost_type == 'Inv':
            return m.costs['Inv'] == \
                sum(m.cap_pro_new[p] *
                    m.process.loc[p]['inv-cost'] *
                    m.process.loc[p]['annuity-factor']
                    for p in m.pro_tuples) + \
                sum(m.cap_tra_new[t] *
                    m.transmission.loc[t]['inv-cost'] *
                    m.transmission.loc[t]['annuity-factor']
                    for t in m.tra_tuples) + \
                sum(m.cap_sto_p_new[s] *
                    m.storage.loc[s]['inv-cost-p'] *
                    m.storage.loc[s]['annuity-factor'] +
                    m.cap_sto_c_new[s] *
                    m.storage.loc[s]['inv-cost-c'] *
                    m.storage.loc[s]['annuity-factor']
                    for s in m.sto_tuples)

        elif cost_type == 'Fix':
            return m.costs['Fix'] == \
                sum(m.cap_pro[p] * m.process.loc[p]['fix-cost']
                    for p in m.pro_tuples) + \
                sum(m.cap_tra[t] * m.transmission.loc[t]['fix-cost']
                    for t in m.tra_tuples) + \
                sum(m.cap_sto_p[s] * m.storage.loc[s]['fix-cost-p'] +
                    m.cap_sto_c[s] * m.storage.loc[s]['fix-cost-c']
                    for s in m.sto_tuples)

        elif cost_type == 'Var':
            return m.costs['Var'] == \
                sum(m.e_pro_out[(tm,) + p] * m.dt *
                    m.process.loc[p]['var-cost'] *
                    m.weight
                    for tm in m.tm for p in m.pro_tuples) + \
                sum(m.e_tra_in[(tm,) + t] * m.dt *
                    m.transmission.loc[t]['var-cost'] *
                    m.weight
                    for tm in m.tm for t in m.tra_tuples) + \
                sum(m.e_sto_con[(tm,) + s] *
                    m.storage.loc[s]['var-cost-c'] * m.weight +
                    (m.e_sto_in[(tm,) + s] + m.e_sto_out[(tm,) + s]) * m.dt *
                    m.storage.loc[s]['var-cost-p'] * m.weight
                    for tm in m.tm for s in m.sto_tuples)

        elif cost_type == 'Fuel':
            return m.costs['Fuel'] == sum(
                m.e_co_stock[(tm,) + c] * m.dt *
                m.commodity.loc[c]['price'] *
                m.weight
                for tm in m.tm for c in m.com_tuples
                if c[1] in m.com_stock)

        else:
            raise NotImplementedError("Unknown cost type.")

    def obj_rule(m):
        return pyomo.summation(m.costs)

    # Equation declaration
    # ====================
    # declarations connect rule functions to the model, specifying
    # the model sets for which the constraints are enforced
    # a constraint with m.{name} automagically binds to the function
    # {name}_rule. If the names don't match, one can link Constraint to
    # function by using the rule keyword. Example:
    # m.every_step = pyomo.Constraint(m.tm, rule=any_function_name)

    # commodity
    m.res_demand = pyomo.Constraint(
        m.tm, m.com_tuples,
        doc='storage + transmission + process + source >= demand')
    m.def_e_co_stock = pyomo.Constraint(
        m.tm, m.com_tuples,
        doc='commodity source term = commodity consumption per timestep')
    m.res_stock_step = pyomo.Constraint(
        m.tm, m.com_tuples,
        doc='commodity source term <= commodity.maxperstep')
    m.res_stock_total = pyomo.Constraint(
        m.com_tuples,
        doc='total commodity source term <= commodity.max')

    # process
    m.def_process_capacity = pyomo.Constraint(
        m.pro_tuples,
        doc='total process capacity = inst-cap + new capacity')
    m.def_process_output = pyomo.Constraint(
        m.tm, m.pro_tuples,
        doc='process output = process input * efficiency')
    m.def_intermittent_supply = pyomo.Constraint(
        m.tm, m.pro_tuples,
        doc='process output = process capacity * supim timeseries')
    m.def_co2_emissions = pyomo.Constraint(
        m.tm, m.pro_tuples,
        doc='process co2 output = process input * process.co2 * weight')
    m.res_process_output_by_capacity = pyomo.Constraint(
        m.tm, m.pro_tuples,
        doc='process output <= total process capacity')
    m.res_process_capacity = pyomo.Constraint(
        m.pro_tuples,
        doc='process.cap-lo <= total process capacity <= process.cap-up')

    # transmission
    m.def_transmission_capacity = pyomo.Constraint(
        m.tra_tuples,
        doc='total transmission capacity = inst-cap + new capacity')
    m.def_transmission_output = pyomo.Constraint(
        m.tm, m.tra_tuples,
        doc='transmission output = transmission input * efficiency')
    m.res_transmission_input_by_capacity = pyomo.Constraint(
        m.tm, m.tra_tuples,
        doc='transmission input <= total transmission capacity')
    m.res_transmission_capacity = pyomo.Constraint(
        m.tra_tuples,
        doc='transmission.cap-lo <= total transmission capacity <= '
            'transmission.cap-up')
    m.res_transmission_symmetry = pyomo.Constraint(
        m.tra_tuples,
        doc='total transmission capacity must be symmetric in both directions')

    # storage
    m.def_storage_state = pyomo.Constraint(
        m.tm, m.sto_tuples,
        doc='storage[t] = storage[t-1] + input - output')
    m.def_storage_power = pyomo.Constraint(
        m.sto_tuples,
        doc='storage power = inst-cap + new power')
    m.def_storage_capacity = pyomo.Constraint(
        m.sto_tuples,
        doc='storage capacity = inst-cap + new capacity')
    m.res_storage_input_by_power = pyomo.Constraint(
        m.tm, m.sto_tuples,
        doc='storage input <= storage power')
    m.res_storage_output_by_power = pyomo.Constraint(
        m.tm, m.sto_tuples,
        doc='storage output <= storage power')
    m.res_storage_state_by_capacity = pyomo.Constraint(
        m.t, m.sto_tuples,
        doc='storage content <= storage capacity')
    m.res_storage_power = pyomo.Constraint(
        m.sto_tuples,
        doc='storage.cap-lo-p <= storage power <= storage.cap-up-p')
    m.res_storage_capacity = pyomo.Constraint(
        m.sto_tuples,
        doc='storage.cap-lo-c <= storage capacity <= storage.cap-up-c')
    m.res_initial_and_final_storage_state = pyomo.Constraint(
        m.t, m.sto_tuples,
        doc='storage content initial == and final >= storage.init * capacity')

    # emissions
    m.res_co2_emission = pyomo.Constraint(
        doc='annual CO2 emissions <= commodity.global.co2.max')

    # costs
    m.def_costs = pyomo.Constraint(
        m.cost_type,
        doc='main cost function by cost type')
    m.obj = pyomo.Objective(
        sense=pyomo.minimize,
        doc='minimize(cost = sum of all cost types)')

    return m


def annuity_factor(n, i):
    """Annuity factor formula.

    Evaluates the annuity factor formula for depreciation duration
    and interest rate. Works also well for equally sized numpy arrays
    of values for n and i.
    
    Args:
        n: depreciation period (years)
        i: interest rate (percent, e.g. 0.06 means 6 %)

    Returns:
        Value of the expression :math:`\\frac{(1+i)^n i}{(1+i)^n - 1}`

    Example:
        >>> round(annuity_factor(20, 0.07), 5)
        0.09439

    """
    return (1+i)**n * i / ((1+i)**n - 1)


def commodity_balance(m, tm, sit, com):
    """Calculate commodity balance at given timestep.

    For a given commodity co and timestep tm, calculate the balance of
    consumed (to process/storage/transmission, counts positive) and provided
    (from process/storage/transmission, counts negative) power. Used as helper
    function in create_model for constraints on demand and stock commodities.

    Args:
        m: the model object
        tm: the timestep
        site: the site
        com: the commodity

    Returns
        balance: net value of consumed (positive) or provided (negative) power

    """
    balance = 0
    for p in m.pro_tuples:
        if p[0] == sit and p[2] == com:
            # usage as input for process increases balance
            balance += m.e_pro_in[(tm,)+p]
        if p[0] == sit and p[3] == com:
            # output from processes decreases balance
            balance -= m.e_pro_out[(tm,)+p]
    for t in m.tra_tuples:
        # exports increase balance
        if t[0] == sit and t[3] == com:
            balance += m.e_tra_in[(tm,)+t]
        # imports decrease balance
        if t[1] == sit and t[3] == com:
            balance -= m.e_tra_out[(tm,)+t]
    for s in m.sto_tuples:
        # usage as input for storage increases consumption
        # output from storage decreases consumption
        if s[0] == sit and s[2] == com:
            balance += m.e_sto_in[(tm,)+s]
            balance -= m.e_sto_out[(tm,)+s]
    return balance


def split_columns(columns, sep='.'):
    """Split columns by separator into MultiIndex.

    Given a list of column labels containing a separator string (default: '.'),
    derive a MulitIndex that is split at the separator string.

    Args:
        columns: list of column labels, containing the separator string
        sep: the separator string (default: '.')
        
    Returns:
        a MultiIndex corresponding to input, with levels split at separator

    Example:
        >>> split_columns(['DE.Elec', 'MA.Elec', 'NO.Wind'])
        MultiIndex(levels=[[u'DE', u'MA', u'NO'], [u'Elec', u'Wind']],
                   labels=[[0, 1, 2], [0, 0, 1]])

    """
    column_tuples = [tuple(col.split('.')) for col in columns]
    return pd.MultiIndex.from_tuples(column_tuples)


def get_entity(instance, name):
    """ Return a DataFrame for an entity in model instance.

    Args:
        instance: a Pyomo ConcreteModel instance
        name: name of a Set, Param, Var, Constraint or Objective

    Returns:
        a single-columned Pandas DataFrame with domain as index
    """

    # retrieve entity, its type and its onset names
    entity = instance.__getattribute__(name)
    labels = _get_onset_names(entity)

    # extract values
    if isinstance(entity, pyomo.Set):
        # Pyomo sets don't have values, only elements
        results = pd.DataFrame([(v, 1) for v in entity.value])

        # for unconstrained sets, the column label is identical to their index
        # hence, make index equal to entity name and append underscore to name
        # (=the later column title) to preserve identical index names for both
        # unconstrained supersets
        if not labels:
            labels = [name]
            name = name+'_'

    elif isinstance(entity, pyomo.Param):
        if entity.dim() > 1:
            results = pd.DataFrame([v[0]+(v[1],) for v in entity.iteritems()])
        else:
            results = pd.DataFrame(entity.iteritems())
    else:
        # create DataFrame
        if entity.dim() > 1:
            # concatenate index tuples with value if entity has
            # multidimensional indices v[0]
            results = pd.DataFrame(
                [v[0]+(v[1].value,) for v in entity.iteritems()])
        else:
            # otherwise, create tuple from scalar index v[0]
            results = pd.DataFrame(
                [(v[0], v[1].value) for v in entity.iteritems()])

    # check for duplicate onset names and append one to several "_" to make
    # them unique, e.g. ['sit', 'sit', 'com'] becomes ['sit', 'sit_', 'com']
    for k, label in enumerate(labels):
        if label in labels[:k]:
            labels[k] = labels[k] + "_"

    # name columns according to labels + entity name
    results.columns = labels + [name]
    results.set_index(labels, inplace=True)

    return results


def get_entities(instance, names):
    """ Return one DataFrame with entities in columns and a common index.

    Works only on entities that share a common domain (set or set_tuple), which
    is used as index of the returned DataFrame.

    Args:
        instance: a Pyomo ConcreteModel instance
        names: list of entity names (as returned by list_entities)

    Returns:
        a Pandas DataFrame with entities as columns and domains as index
    """

    df = pd.DataFrame()
    for name in names:
        other = get_entity(instance, name)

        if df.empty:
            df = other
        else:
            index_names_before = df.index.names

            df = df.join(other, how='outer')

            if index_names_before != df.index.names:
                df.index.names = index_names_before

    return df


def list_entities(instance, entity_type):
    """ Return list of sets, params, variables, constraints or objectives

    Args:
        instance: a Pyomo ConcreteModel object
        entity_type: "set", "par", "var", "con" or "obj"

    Returns:
        DataFrame of entities

    Example:
        >>> data = read_excel('data-example.xlsx')
        >>> model = create_model(data, range(1,25))
        >>> list_entities(model, 'obj')  #doctest: +NORMALIZE_WHITESPACE
                                         Description Domain
        Name
        obj   minimize(cost = sum of all cost types)     []
        [1 rows x 2 columns]

    """

    # helper function to discern entities by type
    def filter_by_type(entity, entity_type):
        if entity_type == 'set':
            return isinstance(entity, pyomo.Set) and not entity.virtual
        elif entity_type == 'par':
            return isinstance(entity, pyomo.Param)
        elif entity_type == 'var':
            return isinstance(entity, pyomo.Var)
        elif entity_type == 'con':
            return isinstance(entity, pyomo.Constraint)
        elif entity_type == 'obj':
            return isinstance(entity, pyomo.Objective)
        else:
            raise ValueError("Unknown entity_type '{}'".format(entity_type))

    # iterate through all model components and keep only 
    iter_entities = instance.__dict__.iteritems()
    entities = sorted(
        (name, entity.doc, _get_onset_names(entity))
        for (name, entity) in iter_entities
        if filter_by_type(entity, entity_type))

    # if something was found, wrap tuples in DataFrame, otherwise return empty
    if entities:
        entities = pd.DataFrame(entities,
                                columns=['Name', 'Description', 'Domain'])
        entities.set_index('Name', inplace=True)
    else:
        entities = pd.DataFrame()
    return entities


def _get_onset_names(entity):
    """
        Example:
            >>> data = read_excel('data-example.xlsx')
            >>> model = create_model(data, range(1,25))
            >>> _get_onset_names(model.e_co_stock)
            ['t', 'sit', 'com', 'com_type']
    """
    # get column titles for entities from domain set names
    labels = []

    if isinstance(entity, pyomo.Set):
        if entity.dimen > 1:
            # N-dimensional set tuples, possibly with nested set tuples within
            if entity.domain:
                domains = entity.domain.set_tuple
            else:
                domains = entity.set_tuple

            for domain_set in domains:
                labels.extend(_get_onset_names(domain_set))

        elif entity.dimen == 1:
            if entity.domain:
                # 1D subset; add domain name
                labels.append(entity.domain.name)
            else:
                # unrestricted set; add entity name
                labels.append(entity.name)
        else:
            # no domain, so no labels needed
            pass

    elif isinstance(entity, (pyomo.Param, pyomo.Var, pyomo.Constraint,
                    pyomo.Objective)):
        if entity.dim() > 0 and entity._index:
            labels = _get_onset_names(entity._index)
        else:
            # zero dimensions, so no onset labels
            pass

    else:
        raise ValueError("Unknown entity type!")

    return labels


def get_constants(instance):
    """Return summary DataFrames for important variables

    Usage:
        costs, cpro, ctra, csto, co2 = get_constants(instance)

    Args:
        instance: a urbs model instance

    Returns:
        (costs, cpro, ctra, csto, co2) tuple
        
    Example:
        >>> import coopr.environ
        >>> from coopr.opt.base import SolverFactory
        >>> data = read_excel('data-example.xlsx')
        >>> model = create_model(data, range(1,25))
        >>> prob = model.create()
        >>> optim = SolverFactory('glpk')
        >>> result = optim.solve(prob)
        >>> prob.load(result)
        True
        >>> get_constants(prob)[-1].sum() <= prob.commodity.loc[
        ...     ('Global', 'CO2', 'Env'), 'max']
        True
    """
    costs = get_entity(instance, 'costs')
    cpro = get_entities(instance, ['cap_pro', 'cap_pro_new'])
    ctra = get_entities(instance, ['cap_tra', 'cap_tra_new'])
    csto = get_entities(instance, ['cap_sto_c', 'cap_sto_c_new',
                                   'cap_sto_p', 'cap_sto_p_new'])

    # co2 timeseries
    co2 = get_entity(instance, 'co2_pro_out')
    co2 = co2.unstack(level=0).sum(axis=1)  # sum co2 emissions over timesteps

    # better labels
    cpro.columns = ['Total', 'New']
    ctra.columns = ['Total', 'New']
    csto.columns = ['C Total', 'C New', 'P Total', 'P New']
    co2.name = 'CO2'

    # better index names
    cpro.index.names = ['sit', 'pro', 'coin', 'cout']
    ctra.index.names = ['sitin', 'sitout', 'tra', 'com']
    co2.index.names = ['sit', 'pro', 'coin', 'cout']

    return costs, cpro, ctra, csto, co2


def get_timeseries(instance, com, sit, timesteps=None):
    """Return DataFrames of all timeseries referring to given commodity

    Usage:
        created, consumed, storage = get_timeseries(instance, co)

    Args:
        instance: a urbs model instance.
        com: a commodity.
        sit: a site.
        timesteps: optional list of timesteps, defaults to modelled timesteps.

    Returns:
        a (created, consumed, storage, imported, exported) tuple of DataFrames 
        timeseries. These are
        
        * created: timeseries of commodity creation, including stock source
        * consumed: timeseries of commodity consumption, including demand
        * storage: timeseries of commodity storage (level, stored, retrieved)
        * imported: timeseries of commodity import (by site)
        * exported: timeseries of commodity export (by site)
    """
    if timesteps is None:
        # default to all simulated timesteps
        timesteps = sorted(get_entity(instance, 'tm').index)

    # DEMAND
    # default to zeros if commodity has no demand, get timeseries
    if com not in instance.com_demand:
        demand = pd.Series(0, index=timesteps)
    else:
        demand = instance.demand.loc[timesteps][sit, com]
    demand.name = 'Demand'

    # STOCK
    eco = get_entity(instance, 'e_co_stock')['e_co_stock'].unstack()['Stock']
    eco = eco.xs(sit, level='sit').unstack().fillna(0)
    try:
        stock = eco.loc[timesteps][com]
    except KeyError:
        stock = pd.Series(0, index=timesteps)
    stock.name = 'Stock'

    # PROCESS
    # group process energies by input/output commodity
    # select all entries of created and consumed desired commodity co
    # and slice to the desired timesteps
    epro = get_entities(instance, ['e_pro_in', 'e_pro_out'])
    epro.index.names = ['tm', 'sit', 'pro', 'coin', 'cout']
    epro = epro.groupby(level=['tm', 'sit', 'coin', 'cout']).sum()
    epro = epro.xs(sit, level='sit')
    try:
        created = epro.xs(com, level='cout')['e_pro_out'].unstack()
        created = created.loc[timesteps]
    except KeyError:
        created = pd.DataFrame(index=timesteps)

    try:
        consumed = epro.xs(com, level='coin')['e_pro_in'].unstack()
        consumed = consumed.loc[timesteps]
    except KeyError:
        consumed = pd.DataFrame(index=timesteps)

    # remove Slack if zero, keep else
    if 'Slack' in created.columns and not created['Slack'].any():
        created.pop('Slack')

    # TRANSMISSION
    etra = get_entities(instance, ['e_tra_in', 'e_tra_out'])
    etra.index.names = ['tm', 'sitin', 'sitout', 'tra', 'com']
    etra = etra.groupby(level=['tm', 'sitin', 'sitout', 'com']).sum()
    etra = etra.xs(com, level='com')

    imported = etra.xs(sit, level='sitout')['e_tra_out'].unstack()
    exported = etra.xs(sit, level='sitin')['e_tra_in'].unstack()

    # STORAGE
    # group storage energies by commodity
    # select all entries with desired commodity co
    esto = get_entities(instance, ['e_sto_con', 'e_sto_in', 'e_sto_out'])
    esto = esto.groupby(level=['t', 'sit', 'com']).sum()
    esto = esto.xs(sit, level='sit')
    try:
        stored = esto.xs(com, level='com')
        stored = stored.loc[timesteps]
        stored.columns = ['Level', 'Stored', 'Retrieved']
    except KeyError:
        stored = pd.DataFrame(0, index=timesteps,
                              columns=['Level', 'Stored', 'Retrieved'])

    # show stock as created
    created = created.join(stock)

    # show demand as consumed
    consumed = consumed.join(demand)

    return created, consumed, stored, imported, exported


def report(instance, filename, commodities, sites):
    """Write result summary to a spreadsheet file

    Args:
        instance: a urbs model instance
        filename: Excel spreadsheet filename, will be overwritten if exists
        commodities: list of commodities for which to create timeseries sheets
        sites: list of sites

    Returns:
        Nothing
    """
    # get the data
    costs, cpro, ctra, csto, co2 = get_constants(instance)

    # create spreadsheet writer object
    with pd.ExcelWriter(filename) as writer:

        # write constants to spreadsheet
        costs.to_excel(writer, 'Costs')
        co2.to_frame('CO2').to_excel(writer, 'CO2')
        cpro.to_excel(writer, 'Process caps')
        ctra.to_excel(writer, 'Transmission caps')
        csto.to_excel(writer, 'Storage caps')
        
        # initialize timeseries tableaus
        energies = []
        timeseries = {}

        # collect timeseries data
        for co in commodities:
            for sit in sites:
                created, consumed, stored, imported, exported = get_timeseries(
                    instance, co, sit)

                overprod = pd.DataFrame(
                    columns=['Overproduction'],
                    data=created.sum(axis=1) - consumed.sum(axis=1) +
                    imported.sum(axis=1) - exported.sum(axis=1) +
                    stored['Retrieved'] - stored['Stored'])

                tableau = pd.concat(
                    [created, consumed, stored, imported, exported, overprod],
                    axis=1,
                    keys=['Created', 'Consumed', 'Storage',
                          'Import from', 'Export to', 'Balance'])
                timeseries[(co, sit)] = tableau.copy()

                # timeseries sums
                sums = pd.concat([created.sum(),
                                  consumed.sum(),
                                  stored.sum().drop('Level'),
                                  imported.sum(),
                                  exported.sum(),
                                  overprod.sum()], axis=0,
                                 keys=['Created', 'Consumed', 'Storage',
                                 'Import', 'Export', 'Balance'])
                energies.append(sums.to_frame("{}.{}".format(co, sit)))

        # concatenate energy sums
        energy = pd.concat(energies, axis=1).fillna(0)
        energy.to_excel(writer, 'Energy sums')

        # write timeseries to individual sheets
        for co in commodities:
            for sit in sites:
                sheet_name = "{}.{} timeseries".format(co, sit)
                timeseries[(co, sit)].to_excel(writer, sheet_name)


def plot(prob, com, sit, timesteps=None):
    """Plot a stacked timeseries of commodity balance and storage.

    Creates a stackplot of the energy balance of a given commodity, together
    with stored energy in a second subplot.

    Args:
        prob: urbs model instance
        com: commodity name to plot
        sit: site name to plot
        timesteps: optional list of  timesteps to plot; default: prob.tm

    Returns:
        fig: figure handle
    """
    import matplotlib.pyplot as plt
    import matplotlib as mpl

    if timesteps is None:
        # default to all simulated timesteps
        timesteps = sorted(get_entity(prob, 'tm').index)

    # FIGURE
    fig = plt.figure(figsize=(16, 8))
    gs = mpl.gridspec.GridSpec(2, 1, height_ratios=[2, 1])

    created, consumed, stored, imported, exported = get_timeseries(
        prob, com, sit, timesteps)

    costs, cpro, ctra, csto, co2 = get_constants(prob)

    # move retrieved/stored storage timeseries to created/consumed and
    # rename storage columns back to 'storage' for color mapping
    created = created.join(stored['Retrieved'])
    consumed = consumed.join(stored['Stored'])
    created.rename(columns={'Retrieved': 'Storage'}, inplace=True)
    consumed.rename(columns={'Stored': 'Storage'}, inplace=True)

    # only keep storage content in storage timeseries
    stored = stored['Level']

    # add imported/exported timeseries
    created = created.join(imported)
    consumed = consumed.join(exported)

    # move demand to its own plot
    demand = consumed.pop('Demand')

    # remove all columns from created which are all-zeros in both created and
    # consumed (except the last one, to prevent a completely empty frame)
    for col in created.columns:
        if not created[col].any() and len(created.columns) > 1:
            if col not in consumed.columns or not consumed[col].any():
                created.pop(col)

    # PLOT CREATED
    ax0 = plt.subplot(gs[0])
    sp0 = ax0.stackplot(created.index, created.as_matrix().T, linewidth=0.15)

    # Unfortunately, stackplot does not support multi-colored legends itself.
    # Therefore, a so-called proxy artist - invisible objects that have the
    # correct color for the legend entry - must be created. Here, Rectangle
    # objects of size (0,0) are used. The technique is explained at
    # http://stackoverflow.com/a/22984060/2375855
    proxy_artists = []
    for k, commodity in enumerate(created.columns):
        commodity_color = to_color(commodity)

        sp0[k].set_facecolor(commodity_color)
        sp0[k].set_edgecolor(to_color('Decoration'))

        proxy_artists.append(mpl.patches.Rectangle(
            (0, 0), 0, 0, facecolor=commodity_color))

    # label
    ax0.set_title('Energy balance of {} in {}'.format(com, sit))
    ax0.set_ylabel('Power (MW)')

    # legend
    lg = ax0.legend(proxy_artists,
                    tuple(created.columns),
                    frameon=False,
                    ncol=created.shape[1],
                    loc='upper center',
                    bbox_to_anchor=(0.5, -0.01))
    plt.setp(lg.get_patches(), edgecolor=to_color('Decoration'),
             linewidth=0.15)
    plt.setp(ax0.get_xticklabels(), visible=False)

    # PLOT CONSUMED
    sp00 = ax0.stackplot(consumed.index, -consumed.as_matrix().T,
                         linewidth=0.15)

    # color
    for k, commodity in enumerate(consumed.columns):
        commodity_color = to_color(commodity)

        sp00[k].set_facecolor(commodity_color)
        sp00[k].set_edgecolor((.5, .5, .5))

    # PLOT DEMAND
    ax0.plot(demand.index, demand.values, linewidth=1.2,
             color=to_color('Demand'))

    # PLOT STORAGE
    ax1 = plt.subplot(gs[1], sharex=ax0)
    sp1 = ax1.stackplot(stored.index, stored.values, linewidth=0.15)

    # color
    sp1[0].set_facecolor(to_color('Storage'))
    sp1[0].set_edgecolor(to_color('Decoration'))

    # labels & y-limits
    ax1.set_xlabel('Time in year (h)')
    ax1.set_ylabel('Energy (MWh)')
    ax1.set_ylim((0, csto.loc[sit, :, com]['C Total'].sum()))

    # make xtick distance duration-dependent
    if len(timesteps) > 26*168:
        steps_between_ticks = 168*4
    elif len(timesteps) > 3*168:
        steps_between_ticks = 168
    elif len(timesteps) > 2 * 24:
        steps_between_ticks = 24
    elif len(timesteps) > 24:
        steps_between_ticks = 6
    else:
        steps_between_ticks = 3
    xticks = timesteps[::steps_between_ticks]

    # set limits and ticks for both axes
    for ax in [ax0, ax1]:
        # ax.set_axis_bgcolor((0, 0, 0, 0))
        plt.setp(ax.spines.values(), color=to_color('Decoration'))
        ax.set_xlim((timesteps[0], timesteps[-1]))
        ax.set_xticks(xticks)
        ax.xaxis.grid(True, 'major', color=to_color('Grid'),
                      linestyle='-')
        ax.yaxis.grid(True, 'major', color=to_color('Grid'),
                      linestyle='-')
        ax.xaxis.set_ticks_position('none')
        ax.yaxis.set_ticks_position('none')
        
        # group 1,000,000 with commas
        group_thousands = mpl.ticker.FuncFormatter(
            lambda x, pos: '{:0,d}'.format(int(x)))
        ax.yaxis.set_major_formatter(group_thousands)

    return fig


def to_color(obj=None):
    """Assign a deterministic pseudo-random color to argument.

    If COLORS[obj] is set, return that. Otherwise, create a random color from
    the hash(obj) representation string. For strings, this value depends only
    on the string content, so that same strings always yield the same color.

    Args:
        obj: any hashable object

    Returns:
        a (r, g, b) color tuple if COLORS[obj] is set, otherwise a hexstring
    """
    if obj is None:
        obj = random()
    try:
        color = tuple(rgb/255.0 for rgb in COLORS[obj])
    except KeyError:
        # random deterministic color
        color = "#{:06x}".format(abs(hash(obj)))[:7]
    return color
