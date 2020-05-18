import React from 'react';
import { makeStyles, Grid, Typography, Paper, List, Button, Box, TextField, Backdrop, CircularProgress } from '@material-ui/core';
import ListItem from '@material-ui/core/ListItem';
import ListItemIcon from '@material-ui/core/ListItemIcon';
import ListItemText from '@material-ui/core/ListItemText';
import ListSubheader from '@material-ui/core/ListSubheader';
import DashboardIcon from '@material-ui/icons/Dashboard';
import ShoppingCartIcon from '@material-ui/icons/ShoppingCart';
import PeopleIcon from '@material-ui/icons/People';
import BarChartIcon from '@material-ui/icons/BarChart';
import LayersIcon from '@material-ui/icons/Layers';
import AssignmentIcon from '@material-ui/icons/Assignment';
import {createState, changeCreateWallet, ALL_OPTIONS, CREATE_CC_WALLET_OPTIONS, CRAETE_EXISTING_CC, CREATE_NEW_CC } from '../modules/createWalletReducer';
import { useDispatch, useSelector } from 'react-redux';
import ArrowBackIosIcon from '@material-ui/icons/ArrowBackIos';
import {useStyles} from './CreateWallet'
import {create_cc_for_colour} from '../modules/message'

export const customStyles = makeStyles((theme) => ({
    input: {
        marginLeft: theme.spacing(3),
        marginRight: theme.spacing(3),
        height: 56,
    },
    send: {
        paddingLeft: '0px',
        marginLeft: theme.spacing(6),
        marginRight: theme.spacing(2),

        height: 56,
        width: 150,
    },
    card: {
        paddingTop: theme.spacing(10),
        height: 200,
    },     
    backdrop: {
        zIndex: theme.zIndex.drawer + 1,
        color: '#fff',
    },
}));

export const CreateExistingCCWallet = () => {
    const classes = useStyles();
    const custom = customStyles();
    const dispatch = useDispatch()
    var colour_string = null
    var open = false
    var pending = useSelector(state => state.create_options.pending)
    var created = useSelector(state => state.create_options.created)


    function goBack() {
        dispatch(changeCreateWallet(CREATE_CC_WALLET_OPTIONS))
    }

    function create() {
        dispatch(createState(true, true))
        const colour = colour_string.value
        dispatch(create_cc_for_colour(colour))
    }

    return (
        <div>
            <div className={classes.cardTitle} >
                <Box display="flex">
                    <Box >
                        <Button onClick={goBack}>
                            <ArrowBackIosIcon> </ArrowBackIosIcon>
                        </Button>
                    </Box>
                    <Box flexGrow={1} className={classes.title}>
                        <Typography component="h6" variant="h6">
                            Create wallet for colour
                        </Typography>
                    </Box>
                </Box>
            </div>
            <div className={custom.card}>
            <Box display="flex">
                <Box flexGrow={1} >
                    <TextField className={custom.input}
                    fullWidth id="outlined-basic" inputRef={(input) => { colour_string = input; }}  label="Colour String" variant="outlined" />
                </Box>
                <Box>
                    <Button onClick={create} className={custom.send} variant="contained" color="primary">
                        Create
                    </Button>
                    <Backdrop className={custom.backdrop} open={open} invisible={false}>
                        <CircularProgress color="inherit" />
                    </Backdrop>
                </Box>
            </Box>
            </div>
            <Backdrop className={classes.backdrop} open={pending && created}>
                    <CircularProgress color="inherit" />
            </Backdrop>
        </div>
    )
}